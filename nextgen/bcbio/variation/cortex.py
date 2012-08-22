"""Perform regional de-novo assembly calling with cortex_var.

Using a pre-mapped set of reads and BED file of regions, performs de-novo
assembly and variant calling against the reference sequence in each region.
This avoids whole genome costs while gaining the advantage of de-novo
prediction.

http://cortexassembler.sourceforge.net/index_cortex_var.html
"""
import os
import glob
import subprocess
import itertools
from contextlib import closing

import pysam
from Bio import Seq
from Bio.SeqIO.QualityIO import FastqGeneralIterator

from bcbio import broad
from bcbio.distributed.transaction import file_transaction
from bcbio.pipeline.shared import subset_variant_regions
from bcbio.utils import file_exists, safe_makedir
from bcbio.variation.genotype import combine_variant_files, write_empty_vcf

def run_cortex(align_bam, ref_file, config, dbsnp=None, region=None,
               out_file=None):
    """Top level entry to regional de-novo based variant calling with cortex_var.
    """
    broad_runner = broad.runner_from_config(config)
    if out_file is None:
        out_file = "%s-cortex.vcf" % os.path.splitext(align_bam)[0]
    if not file_exists(out_file):
        broad_runner.run_fn("picard_index", align_bam)
        variant_regions = config["algorithm"].get("variant_regions", None)
        if not variant_regions:
            raise ValueError("Only regional variant calling with cortex_var is supported. Set variant_regions")
        target_regions = subset_variant_regions(variant_regions, region, out_file)
        with open(target_regions) as in_handle:
            regional_vcfs = [_run_cortex_on_region(x.strip().split("\t")[:3], align_bam,
                                                   ref_file, out_file, config)
                             for x in in_handle]
        combine_variant_files(regional_vcfs, out_file, ref_file, config)
    return out_file

def _run_cortex_on_region(region, align_bam, ref_file, out_file_base, config):
    """Run cortex on a specified chromosome start/end region.
    """
    kmers = [31]
    min_reads = 700
    cortex_dir = config["program"].get("cortex")
    stampy_dir = config["program"].get("stampy")
    vcftools_dir = config["program"].get("vcftools")
    if cortex_dir is None or stampy_dir is None:
        raise ValueError("cortex_var requires path to pre-built cortex and stampy")
    region_str = apply("{0}-{1}-{2}".format, region)
    base_dir = safe_makedir(os.path.join(os.path.dirname(out_file_base), region_str))
    out_vcf_base = os.path.join(base_dir, "{0}-{1}".format(
            os.path.splitext(os.path.basename(out_file_base))[0], region_str))
    out_file = "{0}.vcf".format(out_vcf_base)
    if not file_exists(out_file):
        fastq = _get_fastq_in_region(region, align_bam, out_vcf_base)
        if _count_fastq_reads(fastq, min_reads) < min_reads:
            write_empty_vcf(out_file)
        else:
            local_ref, genome_size = _get_local_ref(region, ref_file, out_vcf_base)
            indexes = _index_local_ref(local_ref, cortex_dir, stampy_dir, kmers)
            cortex_out = _run_cortex(fastq, indexes, {"kmers": kmers, "genome_size": genome_size,
                                                      "sample": _get_sample_name(align_bam)},
                                     out_vcf_base, {"cortex": cortex_dir, "stampy": stampy_dir,
                                                    "vcftools": vcftools_dir},
                                     config)
            if cortex_out:
                _remap_cortex_out(cortex_out, region, out_file)
            else:
                write_empty_vcf(out_file)
    return out_file

def _remap_cortex_out(cortex_out, region, out_file):
    """Remap coordinates in local cortex variant calls to the original global region.
    """
    def _remap_vcf_line(line, contig, start):
        parts = line.split("\t")
        parts[0] = contig
        parts[1] = str(int(parts[1]) + start)
        return "\t".join(parts)
    contig, start, _ = region
    start = int(start) - 1
    with open(cortex_out) as in_handle:
        with open(out_file, "w") as out_handle:
            for line in in_handle:
                if line.startswith("##fileDate"):
                    pass
                elif line.startswith("#"):
                    out_handle.write(line)
                else:
                    out_handle.write(_remap_vcf_line(line, contig, start))

def _run_cortex(fastq, indexes, params, out_base, dirs, config):
    """Run cortex_var run_calls.pl, producing a VCF variant file.
    """
    print out_base
    assert len(params["kmers"]) == 1, "Currently only support single kmer workflow"
    fastaq_index = "{0}.fastaq_index".format(out_base)
    se_fastq_index = "{0}.se_fastq".format(out_base)
    pe_fastq_index = "{0}.pe_fastq".format(out_base)
    reffasta_index = "{0}.list_ref_fasta".format(out_base)
    with open(se_fastq_index, "w") as out_handle:
        out_handle.write(fastq + "\n")
    with open(pe_fastq_index, "w") as out_handle:
        out_handle.write("")
    with open(fastaq_index, "w") as out_handle:
        out_handle.write("{0}\t{1}\t{2}\t{2}\n".format(params["sample"], se_fastq_index,
                                                       pe_fastq_index))
    with open(reffasta_index, "w") as out_handle:
        for x in indexes["fasta"]:
            out_handle.write(x + "\n")
    os.environ["PERL5LIB"] = "{0}:{1}:{2}".format(
        os.path.join(dirs["cortex"], "scripts/calling"),
        os.path.join(dirs["cortex"], "scripts/analyse_variants/bioinf-perl/lib"),
        os.environ.get("PERL5LIB", ""))
    subprocess.check_call(["perl", os.path.join(dirs["cortex"], "scripts", "calling", "run_calls.pl"),
                           "--first_kmer", str(params["kmers"][0]), "--fastaq_index", fastaq_index,
                           "--auto_cleaning", "yes", "--bc", "yes", "--pd", "yes",
                           "--outdir", os.path.dirname(out_base), "--outvcf", os.path.basename(out_base),
                           "--ploidy", str(config["algorithm"].get("ploidy", 2)),
                           "--stampy_hash", indexes["stampy"],
                           "--stampy_bin", os.path.join(dirs["stampy"], "stampy.py"),
                           "--refbindir", os.path.dirname(indexes["cortex"][0]),
                           "--list_ref_fasta",  reffasta_index,
                           "--genome_size", str(params["genome_size"]),
                           "--max_read_len", "10000",
                           "--format", "FASTQ", "--qthresh", "5", "--do_union", "yes",
                           "--mem_height", "17", "--mem_width", "100",
                           "--ref", "CoordinatesAndInCalling", "--workflow", "independent",
                           "--vcftools_dir", dirs["vcftools"],
                           "--logfile", "{0}.logfile,f".format(out_base)])
    final = glob.glob(os.path.join(os.path.dirname(out_base), "vcfs",
                                  "{0}*FINAL*raw.vcf".format(os.path.basename(out_base))))
    # No calls, need to setup an empty file
    if len(final) != 1:
        print "Did not find output VCF file for {0}".format(out_base)
        return None
    else:
        return final[0]

def _get_cortex_binary(kmer, cortex_dir):
    cortex_bin = None
    for check_bin in sorted(glob.glob(os.path.join(cortex_dir, "bin", "cortex_var_*"))):
        kmer_check = int(os.path.basename(check_bin).split("_")[2])
        if kmer_check >= kmer:
            cortex_bin = check_bin
            break
    assert cortex_bin is not None, \
        "Could not find cortex_var executable in %s for kmer %s" % (cortex_dir, kmer)
    return cortex_bin

def _index_local_ref(fasta_file, cortex_dir, stampy_dir, kmers):
    """Pre-index a generated local reference sequence with cortex_var and stampy.
    """
    base_out = os.path.splitext(fasta_file)[0]
    cindexes = []
    for kmer in kmers:
        out_file = "{0}.k{1}.ctx".format(base_out, kmer)
        if not file_exists(out_file):
            file_list = "{0}.se_list".format(base_out)
            with open(file_list, "w") as out_handle:
                out_handle.write(fasta_file + "\n")
            subprocess.check_call([_get_cortex_binary(kmer, cortex_dir),
                                   "--kmer_size", str(kmer), "--mem_height", "17",
                                   "--se_list", file_list, "--format", "FASTA",
                                   "--max_read_len", "10000", "--sample_id", base_out,
                                   "--dump_binary", out_file])
        cindexes.append(out_file)
    if not file_exists("{0}.stidx".format(base_out)):
        subprocess.check_call([os.path.join(stampy_dir, "stampy.py"), "-G",
                               base_out, fasta_file])
        subprocess.check_call([os.path.join(stampy_dir, "stampy.py"), "-g",
                               base_out, "-H", base_out])
    return {"stampy": base_out,
            "cortex": cindexes,
            "fasta": [fasta_file]}

def _get_local_ref(region, ref_file, out_vcf_base):
    """Retrieve a local FASTA file corresponding to the specified region.
    """
    out_file = "{0}.fa".format(out_vcf_base)
    if not file_exists(out_file):
        with closing(pysam.Fastafile(ref_file)) as in_pysam:
            contig, start, end = region
            seq = in_pysam.fetch(contig, int(start) - 1, int(end))
            with open(out_file, "w") as out_handle:
                out_handle.write(">{0}-{1}-{2}\n{3}".format(contig, start, end,
                                                              str(seq)))
    with open(out_file) as in_handle:
        in_handle.readline()
        size = len(in_handle.readline().strip())
    return out_file, size

def _get_fastq_in_region(region, align_bam, out_base):
    """Retrieve fastq files in region as single end.
    Paired end is more complicated since pairs can map off the region, so focus
    on local only assembly since we've previously used paired information for mapping.
    """
    out_file = "{0}.fastq".format(out_base)
    if not file_exists(out_file):
        with closing(pysam.Samfile(align_bam, "rb")) as in_pysam:
            with file_transaction(out_file) as tx_out_file:
                with open(out_file, "w") as out_handle:
                    contig, start, end = region
                    for read in in_pysam.fetch(contig, int(start) - 1, int(end)):
                        seq = Seq.Seq(read.seq)
                        qual = list(read.qual)
                        if read.is_reverse:
                            seq = seq.reverse_complement()
                            qual.reverse()
                        out_handle.write("@{name}\n{seq}\n+\n{qual}\n".format(
                                name=read.qname, seq=str(seq), qual="".join(qual)))
    return out_file

## Utility functions

def _count_fastq_reads(in_fastq, min_reads):
    """Count the number of fastq reads in a file, stopping after reaching min_reads.
    """
    with open(in_fastq) as in_handle:
        items = list(itertools.takewhile(lambda i : i <= min_reads,
                                         (i for i, _ in enumerate(FastqGeneralIterator(in_handle)))))
    return len(items)

def _get_sample_name(align_bam):
    with closing(pysam.Samfile(align_bam, "rb")) as in_pysam:
        return in_pysam.header["RG"][0]["SM"]