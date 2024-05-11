#!/usr/bin/env python
from time import sleep
import struct
import hashlib
import bz2
import sys
import argparse
import bsdiff4
import io
import os
from enlighten import get_manager
import lzma
from multiprocessing import cpu_count
from concurrent.futures import ThreadPoolExecutor, as_completed
import update_metadata_pb2 as um
import zipfile
import http_file

flatten = lambda l: [item for sublist in l for item in sublist]


def u32(x):
    return struct.unpack(">I", x)[0]


def u64(x):
    return struct.unpack(">Q", x)[0]


def verify_contiguous(exts):
    blocks = 0
    for ext in exts:
        if ext.start_block != blocks:
            return False

        blocks += ext.num_blocks

    return True


class Dumper:
    def __init__(
            self, payloadfile, out, diff=None, old=None, images="", workers=cpu_count()
    ):
        self.payloadfile = payloadfile
        self.manager = get_manager()
        self.download_progress = None
        if isinstance(payloadfile, http_file.HttpFile):
            payloadfile.progress_reporter = self.update_download_progress
        self.out = out
        self.diff = diff
        self.old = old
        self.images = images
        self.workers = workers
        try:
            self.parse_metadata()
        except AssertionError:
            # try zip
            with zipfile.ZipFile(self.payloadfile, "r") as zip_file:
                self.payloadfile = zip_file.open("payload.bin", "r")
            self.parse_metadata()
            pass

    def update_download_progress(self, prog, total):
        if self.download_progress is None and prog != total:
            self.download_progress = self.manager.counter(
                total=total,
                desc="download",
                unit="b", leave=False)
        if self.download_progress is not None:
            self.download_progress.update(prog - self.download_progress.count)
            if prog == total:
                self.download_progress.close()
                self.download_progress = None

    def run(self):
        if self.images == "":
            partitions = self.dam.partitions
        else:
            partitions = []
            for image in self.images.split(","):
                image = image.strip()
                found = False
                for dam_part in self.dam.partitions:
                    if dam_part.partition_name == image:
                        partitions.append(dam_part)
                        found = True
                        break
                if not found:
                    print("Partition %s not found in image" % image)

        if len(partitions) == 0:
            print("Not operating on any partitions")
            return 0

        partitions_with_ops = []
        for partition in partitions:
            operations = []
            for operation in partition.operations:
                self.payloadfile.seek(self.data_offset + operation.data_offset)
                operations.append(
                    {
                        "operation": operation,
                        "data": self.payloadfile.read(operation.data_length),
                    }
                )
            partitions_with_ops.append(
                {
                    "partition": partition,
                    "operations": operations,
                }
            )

        self.payloadfile.close()

        self.multiprocess_partitions(partitions_with_ops)
        self.manager.stop()

    def multiprocess_partitions(self, partitions):
        progress_bars = {}

        def update_progress(partition_name, count):
            progress_bars[partition_name].update(count)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            for part in partitions:
                partition_name = part['partition'].partition_name
                progress_bars[partition_name] = self.manager.counter(
                    total=len(part["operations"]),
                    desc=f"{partition_name}",
                    unit="ops",
                    leave=True,
                )

            futures = {executor.submit(self.dump_part, part, update_progress): part for part in partitions}

            for future in as_completed(futures):
                part = futures[future]
                partition_name = part['partition'].partition_name
                try:
                    future.result()
                    progress_bars[partition_name].close()
                except Exception as exc:
                    print(f"{partition_name} - processing generated an exception: {exc}")
                    progress_bars[partition_name].close()

    def parse_metadata(self):
        head_len = 4 + 8 + 8 + 4
        buffer = self.payloadfile.read(head_len)
        assert len(buffer) == head_len
        magic = buffer[:4]
        assert magic == b"CrAU"

        file_format_version = u64(buffer[4:12])
        assert file_format_version == 2

        manifest_size = u64(buffer[12:20])

        metadata_signature_size = 0

        if file_format_version > 1:
            metadata_signature_size = u32(buffer[20:24])

        manifest = self.payloadfile.read(manifest_size)
        self.metadata_signature = self.payloadfile.read(metadata_signature_size)
        self.data_offset = self.payloadfile.tell()

        self.dam = um.DeltaArchiveManifest()
        self.dam.ParseFromString(manifest)
        self.block_size = self.dam.block_size

    def data_for_op(self, operation, out_file, old_file):
        data = operation["data"]
        op = operation["operation"]

        # assert hashlib.sha256(data).digest() == op.data_sha256_hash, 'operation data hash mismatch'

        if op.type == op.REPLACE_XZ:
            dec = lzma.LZMADecompressor()
            data = dec.decompress(data)
            out_file.seek(op.dst_extents[0].start_block * self.block_size)
            out_file.write(data)
        elif op.type == op.REPLACE_BZ:
            dec = bz2.BZ2Decompressor()
            data = dec.decompress(data)
            out_file.seek(op.dst_extents[0].start_block * self.block_size)
            out_file.write(data)
        elif op.type == op.REPLACE:
            out_file.seek(op.dst_extents[0].start_block * self.block_size)
            out_file.write(data)
        elif op.type == op.SOURCE_COPY:
            if not self.diff:
                print("SOURCE_COPY supported only for differential OTA")
                sys.exit(-2)
            out_file.seek(op.dst_extents[0].start_block * self.block_size)
            for ext in op.src_extents:
                old_file.seek(ext.start_block * self.block_size)
                data = old_file.read(ext.num_blocks * self.block_size)
                out_file.write(data)
        elif op.type == op.SOURCE_BSDIFF:
            if not self.diff:
                print("SOURCE_BSDIFF supported only for differential OTA")
                sys.exit(-3)
            out_file.seek(op.dst_extents[0].start_block * self.block_size)
            tmp_buff = io.BytesIO()
            for ext in op.src_extents:
                old_file.seek(ext.start_block * self.block_size)
                old_data = old_file.read(ext.num_blocks * self.block_size)
                tmp_buff.write(old_data)
            tmp_buff.seek(0)
            old_data = tmp_buff.read()
            tmp_buff.seek(0)
            tmp_buff.write(bsdiff4.patch(old_data, data))
            n = 0
            tmp_buff.seek(0)
            for ext in op.dst_extents:
                tmp_buff.seek(n * self.block_size)
                n += ext.num_blocks
                data = tmp_buff.read(ext.num_blocks * self.block_size)
                out_file.seek(ext.start_block * self.block_size)
                out_file.write(data)
        elif op.type == op.ZERO:
            for ext in op.dst_extents:
                out_file.seek(ext.start_block * self.block_size)
                out_file.write(b"\x00" * ext.num_blocks * self.block_size)
        else:
            print("Unsupported type = %d" % op.type)
            sys.exit(-1)

        return data

    def dump_part(self, part, update_callback):
        name = part["partition"].partition_name
        out_file = open("%s/%s.img" % (self.out, name), "wb")
        h = hashlib.sha256()

        if self.diff:
            old_file = open("%s/%s.img" % (self.old, name), "rb")
        else:
            old_file = None

        for op in part["operations"]:
            data = self.data_for_op(op, out_file, old_file)
            update_callback(part["partition"].partition_name, 1)


def main():
    parser = argparse.ArgumentParser(description="OTA payload dumper")
    parser.add_argument(
        "payloadfile", help="payload file name"
    )
    parser.add_argument(
        "--out", default="output", help="output directory (default: 'output')"
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="extract differential OTA",
    )
    parser.add_argument(
        "--old",
        default="old",
        help="directory with original images for differential OTA (default: 'old')",
    )
    parser.add_argument(
        "--partitions",
        default="",
        help="comma separated list of partitions to extract (default: extract all)",
    )
    parser.add_argument(
        "--workers",
        default=cpu_count(),
        type=int,
        help="numer of workers (default: CPU count - %d)" % cpu_count(),
    )
    args = parser.parse_args()

    # Check for --out directory exists
    if not os.path.exists(args.out):
        os.makedirs(args.out)

    payload_file = args.payloadfile
    if payload_file.startswith('http://') or payload_file.startswith("https://"):
        payload_file = http_file.HttpFile(payload_file)
    else:
        payload_file = open(payload_file, 'rb')

    dumper = Dumper(
        payload_file,
        args.out,
        diff=args.diff,
        old=args.old,
        images=args.partitions,
        workers=args.workers,
    )
    dumper.run()

    if isinstance(payload_file, http_file.HttpFile):
        print('\ntotal bytes read from network:', payload_file.total_bytes)


if __name__ == "__main__":
    main()
