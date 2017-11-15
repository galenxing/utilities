#!/usr/bin/env python

import argparse
import logging
import importlib.util
import json
import os
import subprocess

import boto3
import botocore.exceptions


def resource_range(name, min_val, max_val):
    def range_validator(s):
        value = int(s)
        if value < min_val:
            msg = "{} must be at least".format(name, min_val)
            raise argparse.ArgumentTypeError(msg)
        if value > max_val:
            msg = "{} can be at most".format(name, max_val)
            raise argparse.ArgumentTypeError(msg)
        return value

    return range_validator


def get_logger(debug, dryrun):
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # create a logging format
    if dryrun:
        formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - (DRYRUN) - %(message)s'
        )
    else:
        formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    logger.addHandler(handler)

    return logger


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
            prog='aegea_launcher.py',
            description=(
                "Run any script as a batch job\n"
                "e.g. aegea_launcher.py my_bucket/my_scripts "
                "[script name] [script args...]"
            ),
            epilog="See https://github.com/czbiohub/utilities for more examples",
            add_help=False,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # basic usage
    basic_group = parser.add_argument_group('basic arguments')
    basic_group.add_argument(
            's3_script_path',
            help='S3 bucket/path to scripts. e.g. jamestwebber-logs/scripts'
    )
    basic_group.add_argument(
            'script_name',
            help='Name of the script to run, e.g. bcl2fastq.py'
    )
    basic_group.add_argument(
            'script_args',
            help='Script arguments, as a string. e.g. "--taxon mus".'
    )


    # instance requirements
    instance_group = parser.add_argument_group('customize the instance')
    image_group = instance_group.add_mutually_exclusive_group()
    image_group.add_argument('--ecr-image', metavar='ECR',
                             help='ECR image to use for the job')
    image_group.add_argument('--ami',
                             help='AMI to use for the job')

    instance_group.add_argument('--queue', default='aegea_batch',
                                help='Queue to submit the job')
    instance_group.add_argument('--vcpus', default=1,
                                type=resource_range('vcpus', 1, 64),
                                help='Number of vCPUs needed, e.g. 16')
    instance_group.add_argument(
            '--memory', default=4000, type=resource_range('memory', 0, 256000),
            help='Amount of memory needed, in MB, e.g. 16000'
    )
    instance_group.add_argument(
            '--storage', default=None,
            type=resource_range('storage', 500, 16000),
            help='Request additional storage, in GiB (min 500)'
    )
    instance_group.add_argument(
            '--ulimits', metavar='U', default=None, nargs='+',
            help='Change instance ulimits, e.g. nofile:1000000'
    )
    instance_group.add_argument('--environment', metavar='ENV', default=None,
                                nargs='+', help='Set environment variables')

    # other arguments
    other_group = parser.add_argument_group('other options')
    other_group.add_argument('--dryrun', action='store_true',
                             help="Print the command but don't launch the job")
    other_group.add_argument('-u', '--upload', action='store_true',
                             help="Upload the script to S3 before running")
    other_group.add_argument('-d', '--debug', action='store_true',
                             help="Set logging to debug level")
    other_group.add_argument('-t', '--testargs', action='store_true',
                             help="Test the arguments on a local script")
    other_group.add_argument('-h', '--help', action='help',
                             help="show this help message and exit")

    args = parser.parse_args()

    if not args.ecr_image or args.ami:
        args.ecr_image = 'sra_download'

    logger = get_logger(args.debug, args.dryrun)

    script_base = os.path.basename(args.script_name)

    s3_bucket = os.path.split(args.s3_script_path)[0]
    s3_key = os.path.join(os.sep.join(os.path.split(args.s3_script_path)[1:]),
                          script_base)

    logger.debug("Starting S3 client")
    client = boto3.client('s3')

    if args.upload:
        if not os.path.exists(args.script_name):
            raise ValueError("Can't find script: {}".format(args.script_name))

        logger.info("Uploading {} to s3://{}".format(
                args.script_name, os.path.join(s3_bucket, s3_key))
        )
        logger.debug("Filename: {}, Bucket: {}, Key: {}".format(
                args.script_name, s3_bucket, s3_key)
        )
        if not args.dryrun:
            client.upload_file(
                    Filename=args.script_name,
                    Bucket=s3_bucket,
                    Key=s3_key
            )
    else:
        try:
            client.head_object(
                    Bucket=s3_bucket,
                    Key=s3_key
            )
        except botocore.exceptions.ClientError:
            raise ValueError("{} is not on s3, you should upload it.".format(
                    args.script_name))

    if args.testargs:
        if not os.path.exists(args.script_name):
            raise ValueError("Can't find script: {}".format(args.script_name))

        logger.debug('Testing script args')

        module_name = os.path.splitext(os.path.basename(args.script_name))[0]

        logger.debug('Importing script as a module')
        spec = importlib.util.spec_from_file_location(module_name,
                                                      args.script_name)
        script_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script_module)

        if hasattr(script_module, 'get_parser'):
            script_parser = script_module.get_parser()
        else:
            raise ValueError(
                    "{} has no 'get_parser' method, can't test args".format(
                            args.script_name
                    )
            )

        try:
            script_parser.parse_args(args.script_args.split())
        except:
            logger.error("{} failed with the given arg string\n\t{}".format(
                    args.script_name, args.script_args)
            )
            raise

        logger.debug('Script parsed args successfully')

    job_command = "aws s3 cp s3://{} .; chmod 755 {}; ./{} {}".format(
            os.path.join(s3_bucket, s3_key),
            script_base, script_base, args.script_args
    )

    aegea_command = ['aegea', 'batch', 'submit',
                     '--queue', args.queue,
                     '--vcpus', str(args.vcpus),
                     '--memory', str(args.memory)]

    if args.ecr_image:
        aegea_command.extend(['--ecr-image', args.ecr_image])
    elif args.ami:
        aegea_command.extend(['--ami', args.ami])

    if args.storage:
        aegea_command.extend(['--storage', '/mnt={}'.format(args.storage)])

    if args.ulimits:
        aegea_command.extend(['--ulimits', ' '.join(args.ulimits)])

    if args.environment:
        aegea_command.extend(['--environment', ' '.join(args.environment)])

    aegea_command.extend(['--command', '"{}"'.format(job_command)])

    logger.info('executing command:\n\t{}'.format(' '.join(aegea_command)))
    if not args.dryrun:
        output = json.loads(subprocess.check_output(' '.join(aegea_command),
                                                    shell=True))
        logger.info('Launched job with jobId: {}'.format(output['jobId']))
