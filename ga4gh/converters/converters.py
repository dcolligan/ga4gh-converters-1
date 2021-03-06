"""
Provides classes that take protocol requests, send that request to
the server, and write a particular genomics file type with the results.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections

import pysam

import ga4gh.schemas.protocol as protocol


class AbstractConverter(object):
    """
    Abstract base class for converter classes
    """
    def __init__(
            self, container, objectIterator, outputFile, binaryOutput):
        self._container = container
        self._objectIterator = objectIterator
        self._outputFile = outputFile
        self._binaryOutput = binaryOutput


# TODO copied from ga4gh-server; push down into a lower layer
class SamCigar(object):
    """
    Utility class for working with SAM CIGAR strings
    """
    # see http://pysam.readthedocs.org/en/latest/api.html
    # #pysam.AlignedSegment.cigartuples
    cigarStrings = [
        protocol.CigarUnit.ALIGNMENT_MATCH,
        protocol.CigarUnit.INSERT,
        protocol.CigarUnit.DELETE,
        protocol.CigarUnit.SKIP,
        protocol.CigarUnit.CLIP_SOFT,
        protocol.CigarUnit.CLIP_HARD,
        protocol.CigarUnit.PAD,
        protocol.CigarUnit.SEQUENCE_MATCH,
        protocol.CigarUnit.SEQUENCE_MISMATCH,
    ]

    @classmethod
    def ga2int(cls, value):
        for i, cigarString in enumerate(cls.cigarStrings):
            if value == cigarString:
                return i

    @classmethod
    def int2ga(cls, value):
        return cls.cigarStrings[value]


# TODO copied from ga4gh-server; push down into a lower layer
class SamFlags(object):
    """
    Utility class for working with SAM flags
    """
    READ_PAIRED = 0x1
    READ_PROPER_PAIR = 0x2
    READ_UNMAPPED = 0x4
    MATE_UNMAPPED = 0x8
    READ_REVERSE_STRAND = 0x10
    MATE_REVERSE_STRAND = 0x20
    FIRST_IN_PAIR = 0x40
    SECOND_IN_PAIR = 0x80
    SECONDARY_ALIGNMENT = 0x100
    FAILED_QUALITY_CHECK = 0x200
    DUPLICATE_READ = 0x400
    SUPPLEMENTARY_ALIGNMENT = 0x800

    @staticmethod
    def isFlagSet(flagAttr, flag):
        return flagAttr & flag == flag

    @staticmethod
    def setFlag(flagAttr, flag):
        return flagAttr | flag


##############################################################################
# SAM
##############################################################################


class SamException(Exception):
    """
    Something that went wrong during converting a SAM file
    """


class SamConverter(object):
    """
    Converts a requested range from a GA4GH server into a SAM file.
    """
    def __init__(
            self, client, readGroupIds=[], referenceId=None,
            start=None, end=None, outputFileName=None, binaryOutput=False):
        self._client = client
        self._readGroupIds = readGroupIds
        self._reference = self._client.get_reference(referenceId)
        self._start = start
        self._end = end
        self._outputFileName = outputFileName
        self._binaryOutput = binaryOutput

    def convert(self):
        header = self._getHeader()
        targetIds = self._getTargetIds(header)
        # pysam can't write to file streams (except for stdout)
        # http://pysam.readthedocs.org/en/latest/usage.html#using-streams
        if self._binaryOutput:
            flags = "wb"
        else:
            flags = "wh"  # h for header
        fileString = "-"
        if self._outputFileName is not None:
            fileString = self._outputFileName
        alignmentFile = pysam.AlignmentFile(fileString, flags, header=header)
        iterator = self._client.search_reads(
            read_group_ids=self._readGroupIds,
            reference_id=self._reference.id,
            start=self._start,
            end=self._end)
        for read in iterator:
            alignedSegment = SamLine.toAlignedSegment(read, targetIds)
            alignmentFile.write(alignedSegment)
        alignmentFile.close()

    def _getHeader(self):
        # Create header information using self._reference
        header = {
            'HD': {'VN': '1.0'},
            'SQ': [{
                'LN': self._reference.length,
                'SN': self._reference.name
            }]
        }
        return header

    def _getTargetIds(self, header):
        # this seems to be how pysam sets the target ids
        targetIds = collections.defaultdict(int)
        targetId = 0
        if 'SQ' in header:
            headerLines = header['SQ']
            for headerLine in headerLines:
                refName = headerLine['SN']
                targetIds[refName] = targetId
                targetId += 1
        return targetIds


class SamLine(object):
    """
    Methods for processing a line in a SAM file
    """
    _encoding = 'utf8'

    def __init__(self):
        raise SamException("SamLine can't be instantiated")

    @classmethod
    def toAlignedSegment(cls, read, targetIds):
        ret = pysam.AlignedSegment()
        # QNAME
        ret.query_name = read.fragment_name.encode(cls._encoding)
        # SEQ
        ret.query_sequence = read.aligned_sequence.encode(cls._encoding)
        # FLAG
        ret.flag = cls.toSamFlag(read)
        # RNAME
        if read.alignment is not None:
            refName = read.alignment.position.reference_name
            ret.reference_id = targetIds[refName]
        # POS
        if read.alignment is None:
            ret.reference_start = 0
        else:
            ret.reference_start = int(read.alignment.position.position)
        # MAPQ
        if read.alignment is not None:
            ret.mapping_quality = read.alignment.mapping_quality
        # CIGAR
        ret.cigar = cls.toCigar(read)
        # RNEXT
        if read.next_mate_position is None:
            ret.next_reference_id = -1
        else:
            nextRefName = read.next_mate_position.reference_name
            ret.next_reference_id = targetIds[nextRefName]
        # PNEXT
        if read.next_mate_position is None:
            ret.next_reference_start = -1
        else:
            ret.next_reference_start = int(read.next_mate_position.position)
        # TLEN
        ret.template_length = read.fragment_length
        # QUAL
        ret.query_qualities = read.aligned_quality
        ret.tags = cls.toTags(read)
        return ret

    @classmethod
    def toSamFlag(cls, read):
        # based on algorithm here:
        # https://github.com/googlegenomics/readthedocs/
        # blob/master/docs/source/migrating_tips.rst
        flag = 0
        if read.number_reads == 2:
            flag = SamFlags.setFlag(
                flag, SamFlags.READ_PAIRED)
        if not read.improper_placement:
            flag = SamFlags.setFlag(
                flag, SamFlags.READ_PROPER_PAIR)
        if read.alignment is None:
            flag = SamFlags.setFlag(
                flag, SamFlags.READ_UNMAPPED)
        if read.next_mate_position.ByteSize() == 0:  # cleared
            flag = SamFlags.setFlag(
                flag, SamFlags.MATE_UNMAPPED)
        if (read.alignment is not None and
                read.alignment.position.strand ==
                protocol.NEG_STRAND):
            flag = SamFlags.setFlag(
                flag, SamFlags.READ_REVERSE_STRAND)
        if (read.next_mate_position is not None and
                read.next_mate_position.strand == protocol.NEG_STRAND):
            flag = SamFlags.setFlag(
                flag, SamFlags.MATE_REVERSE_STRAND)
        if read.read_number == -1:
            pass
        elif read.read_number == 0:
            flag = SamFlags.setFlag(
                flag, SamFlags.FIRST_IN_PAIR)
        elif read.read_number == 1:
            flag = SamFlags.setFlag(
                flag, SamFlags.SECOND_IN_PAIR)
        else:
            flag = SamFlags.setFlag(
                flag, SamFlags.FIRST_IN_PAIR)
            flag = SamFlags.setFlag(
                flag, SamFlags.SECOND_IN_PAIR)
        if read.secondary_alignment:
            flag = SamFlags.setFlag(
                flag, SamFlags.SECONDARY_ALIGNMENT)
        if read.failed_vendor_quality_checks:
            flag = SamFlags.setFlag(
                flag, SamFlags.FAILED_QUALITY_CHECK)
        if read.duplicate_fragment:
            flag = SamFlags.setFlag(
                flag, SamFlags.DUPLICATE_READ)
        if read.supplementary_alignment:
            flag = SamFlags.setFlag(
                flag, SamFlags.SUPPLEMENTARY_ALIGNMENT)
        return flag

    @classmethod
    def toCigar(cls, read):
        cigarTuples = []
        if read.alignment is not None:
            for gaCigarUnit in read.alignment.cigar:
                operation = SamCigar.ga2int(gaCigarUnit.operation)
                length = int(gaCigarUnit.operation_length)
                cigarTuple = (operation, length)
                cigarTuples.append(cigarTuple)
        return tuple(cigarTuples)

    @classmethod
    def _parseTagValue(cls, value):
        if len(value) > 1:
            return [protocol.getValueFromValue(v) for v in value]
        elif isinstance(
                protocol.getValueFromValue(value[0]), (int, float, long)):
            return protocol.getValueFromValue(value[0])
        else:
            return str(
                protocol.getValueFromValue(value[0])).encode(cls._encoding)

    @classmethod
    def toTags(cls, read):
        tags = []
        for tag, values in read.attributes.attr.items():
            val = cls._parseTagValue(list(values.values))
            tags.append((tag.encode(cls._encoding), val))
        retval = tuple(tags)
        return retval


##############################################################################
# VCF
##############################################################################


class VcfException(Exception):
    pass


class VcfConverter(AbstractConverter):
    """
    Converts the Variants represented by a SearchVariantsRequest into
    VCF format using pysam.
    """
    def _writeHeader(self):
        variantSet = self._container
        # TODO convert this into pysam types and write to the output file.
        # For now, just print out some stuff to demonstrate how to get the
        # attributes we have.
        print("ID = ", variantSet.id)
        print("Dataset ID = ", variantSet.datasetId)
        print("Metadata = ")
        for metadata in variantSet.metadata:
            print("\t", metadata)

    def _writeBody(self):
        for variant in self._objectIterator:
            # TODO convert each variant object into pysam objects and write to
            # the output file. For now, just print the first variant and break.
            print(variant)
            break

    def convert(self):
        """
        Run the conversion process.
        """
        # TODO allocate the pysam VCF object which can be used for the
        # conversion process. See the convert method for ga2sam above.
        self._writeHeader()
        self._writeBody()
