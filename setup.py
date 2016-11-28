from setuptools import setup
import setuptools.command.install as install
import setuptools.command.sdist as sdist

setup(name = 'aws-deployer',
      author = 'Green Mars Consulting',
      author_email = 'dej@greenmars.consulting',
      cmdclass = {
          'install': install,
          'sdist': sdist,
      },
      description = 'Deploy applications to CloudFormation',
      install_requires = [
          'boto3',
          'botocore',
      ],
      license = 'GPLv3.0',
      long_description = "A one-click script for deploying code to CloudFormation-based applications / Support for nested CloudFormation stacks/templates / Support for uploading static content to S3 and creating CloudFront distributions for static content / Plugin for generating / executing database migrations for a release / All artifacts of each deployment/release are stored in S3 and version-tagged: Tarball of code, CloudFormation template, Static files folder",
      url = 'https://greenmars.consulting',
      version = VERSION)