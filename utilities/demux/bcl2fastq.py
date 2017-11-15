#!/usr/bin/env python

import argparse
import glob
import logging
import os
import re
import subprocess
import sys


BCL2FASTQ = 'bcl2fastq'

S3_RETRY = 5
S3_LOG_DIR = 's3://jamestwebber-logs/bcl2fastq_logs/'


def get_default_requirements():
    return argparse.Namespace(vcpus=64, memory=256000, storage=1200,
                              ulimits=['nofile:1000000'])


def get_parser():
    parser = argparse.ArgumentParser(prog='bcl2fastq.py')

    parser.add_argument('--exp_id')

    parser.add_argument('--s3_input_dir',
                        default='s3://czbiohub-seqbot/bcl')
    parser.add_argument('--s3_output_dir',
                        default='s3://czbiohub-seqbot/fastqs')
    parser.add_argument('--s3_report_dir',
                        default='s3://czbiohub-seqbot/reports')
    parser.add_argument('--s3_sample_sheet_dir',
                        default='s3://czbiohub-seqbot/sample-sheets')

    parser.add_argument('--star_structure', action='store_true')
    parser.add_argument('--bcl2fastq_options', default='--no-lane-splitting')
    parser.add_argument('--skip_undetermined', action='store_true')

    parser.add_argument('--sample_sheet_name', default=None,
                        help='Defaults to [exp_id].csv')
    parser.add_argument('--root_dir', default='/mnt')

    return parser


def log_command(logger, command, **kwargs):
    logger.info(' '.join(command))
    output = subprocess.check_output(' '.join(command), **kwargs)
    logger.debug(output)


def main(logger):
    parser = get_parser()

    args = parser.parse_args()

    if os.environ.get('AWS_BATCH_JOB_ID'):
        args.root_dir = os.path.join(args.root_dir,
                                     os.environ['AWS_BATCH_JOB_ID'])


    if args.sample_sheet_name is None:
        args.sample_sheet_name = '{}.csv'.format(args.exp_id)

    # local directories
    result_path = os.path.join(args.root_dir, 'data', 'hca', args.exp_id)
    bcl_path = os.path.join(result_path, 'bcl')
    output_path = os.path.join(result_path, 'fastqs')

    # download sample sheet
    os.makedirs(result_path)
    os.mkdir(bcl_path)



    command = ['aws', 's3', 'cp',
               os.path.join(args.s3_sample_sheet_dir, args.sample_sheet_name),
               result_path]
    for i in range(S3_RETRY):
        try:
            log_command(logger, command, shell=True)
            break
        except subprocess.CalledProcessError:
            logger.info("retrying s3 copy")
    else:
        raise RuntimeError("couldn't download sample sheet {}".format(
                os.path.join(args.s3_sample_sheet_dir, args.sample_sheet_name))
        )



    # download the bcl files
    command = ['aws', 's3', 'sync',
               os.path.join(args.s3_input_dir, args.exp_id), bcl_path]
    for i in range(S3_RETRY):
        try:
            log_command(logger, command, shell=True)
            break
        except subprocess.CalledProcessError:
            logger.info("retrying s3 sync bcl")
    else:
        raise RuntimeError("couldn't sync {}".format(
                os.path.join(args.s3_input_dir, args.exp_id))
        )


    command = ('while true;'
               ' do echo "memory usage" `cat /sys/fs/cgroup/memory/memory.usage_in_bytes`;'
               ' echo "disk usage" `df -h | grep "/mnt"`;'
               ' sleep 90;'
               ' done')
    p = subprocess.Popen([command], shell=True)

    # Run bcl2 fastq
    command = [BCL2FASTQ, args.bcl2fastq_options,
               '--sample-sheet', os.path.join(result_path,
                                              args.sample_sheet_name),
               '-R', bcl_path, '-o', output_path]
    log_command(logger, command, shell=True)

    # fix directory structure of the files *before* sync!
    fastqgz_files = glob.glob(os.path.join(output_path, '*fastq.gz'))
    logger.debug('all fastq.gz files\n{}\n\n'.format('\n'.join(fastqgz_files)))

    for fastq_file in fastqgz_files:
        if (args.skip_undetermined
            and os.path.basename(fastq_file).startswith('Undetermined')):
            logger.info("removing {}".format(os.path.basename(fastq_file)))
            os.remove(fastq_file)
        elif args.star_structure:
            m = re.match("(.+)(_R[12]_001.fastq.gz)",
                         os.path.basename(fastq_file))
            if m:
                sample = m.group(1)
                if not os.path.exists(os.path.join(output_path, sample)):
                    logger.debug("creating {}".format(
                            os.path.join(output_path, sample))
                    )
                    os.mkdir(os.path.join(output_path, sample))
                logger.debug("moving {}".format(fastq_file))
                os.rename(fastq_file, os.path.join(
                        output_path, sample, os.path.basename(fastq_file)
                ))
            else:
                logger.warning("Warning: regex didn't match {}".format(fastq_file))

    sys.stdout.flush()

    # upload fastq files to destination folder
    command = ['aws', 's3', 'sync', output_path,
               os.path.join(args.s3_output_dir, args.exp_id, 'rawdata'),
               '--exclude', '"*"', '--include', '"*fastq.gz"']
    for i in range(S3_RETRY):
        try:
            log_command(logger, command, shell=True)
            break
        except subprocess.CalledProcessError:
            logger.info("retrying sync fastq")
    else:
        raise RuntimeError("couldn't sync fastqs")


    # check fastq upload
    command = ['aws', 's3', 'ls', '--recursive',
               os.path.join(args.s3_output_dir, args.exp_id, 'rawdata')]
    log_command(logger, command, shell=True)


    # Move reports data back to S3
    reports_path = subprocess.check_output(
            "ls -d {}".format(os.path.join(output_path, 'Reports', 'html', '*',
                                           'all', 'all', 'all')),
            shell=True).rstrip()
    command = ['aws', 's3', 'cp', reports_path,
               os.path.join(args.s3_report_dir, args.exp_id),
               '--recursive']
    for i in range(S3_RETRY):
        try:
            log_command(logger, command, shell=True)
            break
        except subprocess.CalledProcessError:
            logger.info("retrying cp reports")
    else:
        raise RuntimeError("couldn't cp reports")

    p.kill()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    mainlogger = logging.getLogger(__name__)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)

    # create a logging format
    formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    stream_handler.setFormatter(formatter)

    mainlogger.addHandler(stream_handler)

    if os.environ.get('AWS_BATCH_JOB_ID'):
        log_file = '{}.log'.format(os.environ['AWS_BATCH_JOB_ID'])
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)

        # add the handlers to the logger
        mainlogger.addHandler(file_handler)
    else:
        log_file = None

    try:
        main(mainlogger)
    except:
        mainlogger.info("An exception occurred", exc_info=True)
        raise
    finally:
        if log_file:
            log_cmd = 'aws s3 cp {} {}'.format(log_file, S3_LOG_DIR)
            mainlogger.info(log_cmd)

            file_handler.close()
            subprocess.check_output(log_cmd, shell=True)