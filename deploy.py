#!/usr/bin/env python
# Copyright (c) 2014 - 2017 - Dave Jackson <dej@greenmars.consulting>
# 
# All rights reserved.

# You may copy, distribute and modify the software as long as you track
# changes/dates in source files. Any modifications to or software including
# (via compiler) GPL-licensed code must also be made available under the
# GPL along with build & install instructions.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import os
import time
import logging
import uuid
import urllib2
import yaml
from argparse import ArgumentParser, FileType
from distutils.core import run_setup
from sys import exit
from os import environ
from os.path import basename
import boto3
from setuptools import setup, find_packages
from functools import partial
from contextlib import closing
from time import strftime
from pprint import pprint
from boto3.s3.transfer import S3Transfer
from botocore.exceptions import ClientError
from deploylib import DeployLib

logging.basicConfig(level=logging.INFO)

"""
Variables to abstract out:
static bucket name format and value (needs to include stack name, but otherwise can be whatever client wants)
build bucket name format and value (needs to include stack name, but otherwise can be whatever client wants)
    this is per-stack, the bucket where tarballs and cloudformation templates go
app dest path - can be whatever client wants. It's just the root folder name for release tarballs within app bucket
template dest path - can be whatever client wants. It's just the root folder name for cloudformation templates within app bucket
root

application source template parameter name - how will the tarball url be referenced? (default ApplicationSource)
release id template parameter name - how will the release id be referenced from within cloudformation? (default ReleaseID)
release notes template parameter name - how will the release notes text be specified (default ReleaseNotes)

environment name template parameter name - how to reference the stack name
    Not needed, can simply use AWS::StackName? Or is there a reason we created this...

build bucket name template parameter name - how will the build bucket name be referenced (default BuildBucketName)
build bucket ARN param name - how will the build bucket arn be referenced? (default BuildBucketAccessArn)

setup.py params:
    script name (defaults to setup.py)
    script args (defaults to ["sdist"])
    author
    author email
    url
    package exclusions
    entry points
    
    e.g.:
            script_name = "setup.py",
            script_args = ["sdist"],
            name = self.pkg_name,
            version = self.pkg_ver,
            author = "Dave Jackson",
            author_email = "dej@greenmars.consulting",
            url = "http://www.adaptrm.com",
            packages = find_packages(exclude=("static"),),
            include_package_data = True,
            zip_safe = False,
            entry_points = {
                "console_scripts": [
                    "arm-queue-processor = arm.run.queue_processor:main"
                ]
            },

static folder exclusions (i.e., do not upload folders X, Y, Z...)
cloudformation template root path (e.g., arm_app/conf/cfn in the case of AdaptRM)
"""

class AppDeployer(object):
    # APP_DEST_BUCKET_SUFFIX = "build-arm-com"
    # STATIC_DEST_BUCKET_PREFIX = 'arm-static'
    # APP_DEST_PATH = "app"
    # TEMPLATE_DEST_PATH = "cfn-configs"

    ROOT_TEMPLATE_NAME = 'arm-cfn-root.json'
    QUEUE_TEMPLATE_NAME = 'arm-cfn-queue-processor.json'

    MIME_TYPES = {
        '.map': 'application/json',
        '.swf': 'application/x-shockwave-flash',
        '.js': 'application/javascript',
        '.woff': 'application/x-font-woff',
        '.otf': 'application/x-font-otf',
        '.eot': 'application/x-font-eot',
        '.ttf': 'application/x-font-ttf',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.svg': 'image/svg+xml',
        '.gif': 'image/gif',
        '.ico': 'image/x-icon',
        '.css': 'text/css'
    }
    
    def __init__(self, dry_run, verbose,
                 config_path, deploy_app,
                 template, template_url, parameters,
                 product, stamp, blessed, stack_name,
                 no_db_migrations, db_migrator, no_static, static_src_root,
                 update_distro, change_cloudfront_origin):
        
        self.stack_name = stack_name
        
        if os.path.exists(config_path) and os.path.isfile(config_path):
            try:
                with open(config_path, 'rb') as f:
                    self.deploy_configs = yaml.load(f)
            except Exception, ex:
                import traceback
                raise Exception("Config path %s could not be loaded beacuse: %s" % (config_path, traceback.format_exc()))
        else:
            raise Exception("Config path %s is not valid" % config_path)

        ### Load stack-specific vars
        if 'stack-vars-root' in self.deploy_configs:
            stack_vars_root_path = self.deploy_configs['stack-vars-root']
            stack_vars_path = os.path.join(stack_vars_root_path, "%s-deploy.yaml" % self.stack_name)
            if os.path.exists(stack_vars_path):
                with open(stack_vars_path, "rb") as f:
                    conf_dict = yaml.load(f)
                    self.stack_vars = conf_dict['%s-vars' % self.stack_name]
            else:
                logging.warn("Could not find path for stack-specific variables: %s" % stack_vars_path)
                self.stack_vars = {}
        else:
            self.stack_vars = {}
        
        self.dry_run = dry_run
        self.verbose = verbose
        self.deploy_app = deploy_app
        self.template = template
        self.template_url = template_url
        self.parameters = parameters
        
        if not no_db_migrations:
            self.db_migrator = db_migrator
            if not self.db_migrator:
                if 'db-migrator' in self.deploy_configs:
                    self.db_migrator = self.deploy_configs['db-migrator']
        else:
            self.db_migrator = None
        self.product = product
        self.stamp = stamp
        self.blessed = blessed
        self.stack_name = stack_name
        self.no_static = no_static
        
        if static_src_root:
            self.static_src_root = static_src_root
        else:
            if 'static-src-root' in self.deploy_configs:
                self.static_src_root = self.deploy_configs['static-src-root']
            else:
                logging.warn("No static src root specified, cannot do static deploy.".upper())
                self.no_static = True

        """
        Allowable combinations:
        --deploy-app (will automatically include --upload-content)[, --update-distro]
        --change-cloudfront-origin <release-id> (replaces update-distro and revert-distro as a stand-alone command)
        """
        self.upload_content = self.deploy_app
        if self.deploy_app:
            self.update_distro = update_distro
            self.revert_distro = None
        else:
            # Ignore
            self.update_distro = False
            self.revert_distro = change_cloudfront_origin
        
        self.deploy_lib = DeployLib(self.product, self.db_migrator)
        (self.release_id, self.pkg_name, self.pkg_ver) = \
            self.deploy_lib.gen_release_id(self.stack_name, self.stamp, is_blessed=self.blessed)
    
    def get_app_bucket_name(self):
        # return "%s-%s" % (self.stack_name, self.APP_DEST_BUCKET_SUFFIX)
        return self.deploy_configs['app-bucket-format'] % {'stack_name': self.stack_name}
    
    def get_static_bucket_name(self):
        # return "%s-%s" % (self.STATIC_DEST_BUCKET_PREFIX, self.stack_name)
        return self.deploy_configs['static-bucket-format'] % {'stack_name': self.stack_name}
    
    def get_mime_type(self, path):
        basename, ext = os.path.splitext(path)
        if ext in self.MIME_TYPES:
            return self.MIME_TYPES[ext]
        else:
            raise Exception("Unknown mime type for [%s]. Please update the MIME_TYPES mapping at the top of this script file." % ext)

    def get_distro_property(self, distro, *args):
        last_element = distro
        for a in args:
            if type(last_element) == dict:
                if a in last_element:
                    last_element = last_element[a]
                else:
                    return None
            elif type(last_element) == list:
                if type(a) != int or a >= len(last_element):
                    return None
                else:
                    last_element = last_element[a]
        
        return last_element

    def get_dist_for_stack(self):
        bucket_name = self.get_static_bucket_name()
        
        # 1. Get all distributions
        # 2. Find one whose origin starts with 'arm-static-<stack>'
        origin_prefix = bucket_name
        cf = boto3.client('cloudfront')
    
        dist_dict = cf.list_distributions()
        if 'DistributionList' in dist_dict:
            if 'Items' in dist_dict['DistributionList']:
                for d in dist_dict['DistributionList']['Items']:
                    dist_id = d['Id']
                    if 'Origins' in d:
                        if 'Items' in d['Origins']:
                            if d['Origins']['Items']:
                                first_origin = d['Origins']['Items'][0]
                                domain_name = first_origin['DomainName']
                                if domain_name.startswith(origin_prefix):
                                    return cf.get_distribution(Id=dist_id)
        return None

    def build(self):
        setup_params = self.deploy_configs['setup-parameters']
        
        if 'search-path-exclusions' in setup_params:
            search_exclusions = setup_params['search-path-exclusions']
        else:
            search_exclusions = []
        
        if 'console-scripts' in setup_params:
            console_scripts_formatted = map(lambda x: "%s = %s" % (x[0], x[1]), setup_params['console-scripts'].items())
        else:
            console_scripts_formatted = []
        
        dist = setup(
            script_name = "setup.py",
            script_args = ["sdist"],
            name = self.pkg_name,
            version = self.pkg_ver,
            author = setup_params['author-name'],
            author_email = setup_params['author-email'],
            url = setup_params['product-url'],
            packages = find_packages(exclude=search_exclusions,),
            include_package_data = True,
            zip_safe = False,
            entry_points = {
                "console_scripts": console_scripts_formatted
            },
        )
        
        for filetype, _, filename in dist.dist_files:
           if filetype == "sdist": return filename
    
    def make_app_s3_key(self, filename, url_encode=False):
        encoded_fname = basename(filename)
        if url_encode:
            encoded_fname = urllib2.quote(basename(filename))
        return "/".join([self.deploy_configs['app-releases-path'], encoded_fname])  
        # return "/".join([self.APP_DEST_PATH, encoded_fname])
    
    def make_template_s3_key(self, filename, url_encode=False):
        return "/".join([self.deploy_configs['cfn-template-releases-path'], "_".join([self.release_id, basename(filename)])])
        # return "/".join([self.TEMPLATE_DEST_PATH, "_".join([self.release_id, basename(filename)])])
    
    def make_static_s3_key(self, filename, url_encode=False, prefix=''):
        # Find part of path after static src root
        relative_path = filename[len(self.static_src_root):]
        if relative_path.startswith(os.path.sep):
            relative_path = relative_path[1:]
        
        path_parts = relative_path.split(os.path.sep)
        
        dest_path_parts = [self.release_id] + [prefix] + path_parts
    
        # Key should be <release-id>/static/<remainder>
        keyname = "/".join(dest_path_parts)
        
        return keyname
    

    def upload(self, bucket_name, filename, content_type, key_maker):
        client = boto3.client('s3')
        transfer = S3Transfer(client)
        
        url_encoded_keyname = key_maker(filename, url_encode=True)
        raw_keyname = key_maker(filename, url_encode=False)
        
        logging.info("About to upload file to S3 with key: %s" % raw_keyname)

        transfer.upload_file(filename, bucket_name, raw_keyname, extra_args={'ContentType': content_type, 'CacheControl': 'max-age=86400'})
        
        ret_url = "".join(["http://", bucket_name, ".s3.amazonaws.com/", url_encoded_keyname])

        msg = "Uploaded file to [%s]" % ret_url
        if self.verbose:
            print msg
        logging.info(msg)

        return ret_url
    
    def upload_app(self, filename):
        bucket_name = self.get_app_bucket_name()
        return self.upload(bucket_name, filename,
                           content_type='application/octet-stream',
                           key_maker=self.make_app_s3_key)
    
    def upload_template(self, filename):
        bucket_name = self.get_app_bucket_name()
        return self.upload(bucket_name, filename,
                           content_type='application/json',
                           key_maker=self.make_template_s3_key)

    def upload_static(self, src_path):
        content_type = None
        try:
            content_type = self.get_mime_type(src_path)
        except:
            logging.warn("Skipping upload of [%s], mime type could not be determined." % src_path)
            
        prefix = ''
        if 'static-prefix' in self.deploy_configs:
            prefix = self.deploy_configs['static-prefix']
        keyname = self.make_static_s3_key(src_path, prefix=prefix)
        
        if self.dry_run:
            if content_type:
                msg = "Would upload stack [%s] with file [%s] to key [%s], but in dry run mode." % (self.stack_name,
                                                                                                    src_path, keyname)
                if self.verbose:
                    print msg
                logging.info(msg)
        else:
            bucket_name = self.get_static_bucket_name()
            
            if content_type:
                self.upload(bucket_name, src_path, content_type, key_maker=self.make_static_s3_key)
        
    def params_as_dict(self, params):
        pdict = {}
        for p in params:
            pdict[p['ParameterKey']] = p['ParameterValue']
        
        return pdict
    
    def dict_as_cfn_params(self, param_dict):
        cfn_params = []
        for key, val in param_dict.items():
            cfn_params.append({
                'ParameterKey': key,
                'ParameterValue': val,
                'UsePreviousValue': False
            })
        
        return cfn_params
    

    def cfndeploy(self, template_url=None, parameters=None):
        params = {}
        cfn_client = boto3.client('cloudformation')
    
        try:
            response = cfn_client.describe_stacks(StackName=self.stack_name)
            stacks = response['Stacks']
            stack_params = self.params_as_dict(stacks[0]['Parameters'])

            assert len(stacks) == 1
            params.update(stack_params)
            op = partial(cfn_client.update_stack,
                        StackName=self.stack_name,
                        TemplateURL=template_url,
                        Capabilities=["CAPABILITY_IAM"],
                        UsePreviousTemplate=False)
        except:
            logging.exception("Exception thrown trying to describe stack")
            op = partial(cfn_client.create_stack,
                        StackName=self.stack_name,
                        TemplateURL=template_url,
                        Capabilities=["CAPABILITY_IAM"],
                        OnFailure='DO_NOTHING')
            print "Problem finding stack with name[%s]" % self.stack_name
        params.update(parameters)
        logging.info(op)
        pprint(params)
        
        cfn_params = self.dict_as_cfn_params(params)
        
        response = op(Parameters=cfn_params)
        logging.info(response)
        
    def get_dry_run_str(self):
        if self.dry_run:
            return "DRYRUN:"
        else:
            return "LIVE DEPLOY:"    

    def do_update_distro(self):
        bucket_name = self.get_static_bucket_name()
        distro = self.get_dist_for_stack()
        distro_id = distro['Distribution']['Id']
        etag = distro['ETag']
        
        use_release_id = self.release_id
        if self.revert_distro:
            use_release_id = self.revert_distro
        
        new_origin_id = 'S3-%s/%s' % (bucket_name, use_release_id)
        
        dist_conf = distro['Distribution']['DistributionConfig']
        dist_conf['DefaultCacheBehavior']['TargetOriginId'] = new_origin_id
        dist_conf['DefaultCacheBehavior']['Compress'] = True
        for origin in dist_conf['Origins']['Items']:
            origin['Id'] = new_origin_id
            origin['OriginPath'] = '/%s' % use_release_id
            
        cf_client = boto3.client('cloudfront')
        cf_client.update_distribution(DistributionConfig=dist_conf,
                               Id=distro_id,
                               IfMatch=etag)
        
        inv_response = cf_client.create_invalidation(
            DistributionId=distro_id,
            InvalidationBatch={
                'Paths': {
                    'Quantity': 1,
                    'Items': [
                        '/*'
                    ]
                },
                'CallerReference': uuid.uuid4().hex
            }
        )
        
    def deploy_static(self):
        logging.info("Deploying static content for stack=[%s]" % self.stack_name)
        
        # Find current distro and origin path for stack
        distro = self.get_dist_for_stack()
        curr_origin_path = self.get_distro_property(distro, 'Distribution', 'DistributionConfig', 'Origins', 'Items', 0, 'OriginPath')
        if curr_origin_path is None:
            msg = "Problem finding curr origin path for stack=%s" % self.stack_name
            logging.error(msg)
            exit(1)
    
        # First show releases in bucket
        release_list = []
        
        release_exists = False
        
        # Indicate which is the current one
        s3 = boto3.resource('s3')
        bucket_name = self.get_static_bucket_name()
        bucket = s3.Bucket(bucket_name)
        result = bucket.meta.client.list_objects(Bucket=bucket.name, Delimiter='/')
        prefixes = result.get('CommonPrefixes')
        if prefixes:
            for o in prefixes:
                release = o.get('Prefix')
                if curr_origin_path.strip('/') in release.strip('/'):
                    release_list.append("%s (current)" % release)
                else:
                    release_list.append(release)
                if self.release_id in release:
                    release_exists = True
        
        msg = "Static releases in stack [%s]:\n\t%s" % (self.stack_name, "\n\t".join(release_list))
        logging.info(msg)
        
        if self.revert_distro:
            try:
                if self.dry_run:
                    msg = "Would revert stack [%s] distro to release id [%s], but in dry run mode." % (self.stack_name, self.revert_distro)
                    logging.info(msg)
                else:
                    # 4. Update the distro to point to the new origin
                    self.do_update_distro()
            except ClientError, ex:
                if ex.response['Error']['Code'] == 'AccessDenied':
                    msg = "usage: BOTO_CONFIG=<your credentials file path> python deploy_static.py"
                    logging.info(msg)
            exit(0)
        
        # Give an error if release id specified already exists in bucket
        if self.upload_content and release_exists:
            msg = "The release id you have specified (%s) already exists. \nYou can only upload static content to a release id that does not exist yet." % self.release_id
            logging.error(msg)
            exit(1)
        
        # Order of operations:
        # 1. OS walk over static files
        if self.upload_content:
            try:
                if self.dry_run:
                    msg = "Would upload contents of %s to stack %s and release id %s, but in dry run mode." % (self.static_src_root, self.stack_name, self.release_id)
                    logging.info(msg)
                
                for root, dirs, files in os.walk(self.static_src_root):
                    for path in files:
                        fpath = os.path.join(root, path)
                        
                        if 'static-folder-exclusions' in self.deploy_configs:
                            static_exclusions = self.deploy_configs['static-folder-exclusions']
                        else:
                            static_exclusions = []
                        
                        static_exclusions = map(lambda x: os.path.join(self.static_src_root, x), static_exclusions)
                        if os.path.dirname(fpath) in static_exclusions:
                            logging.info("Excluding folder: %s" % fpath)
                        else:
                            # print "fpath: %s" % fpath
                            # 2. Upload files to $new_origin (which is currently a new folder in S3)
                            self.upload_static(src_path=fpath)
            except ClientError, ex:
                print ex
                if ex.response['Error']['Code'] == 'AccessDenied':
                    msg = "usage: BOTO_CONFIG=<your credentials file path> python deploy_static.py"
                    logging.info(msg)
    
        
        if self.update_distro:
            try:
                if self.dry_run:
                    msg = "Would update stack [%s] distro to release id [%s], but in dry run mode." % (self.stack_name, self.release_id)
                    logging.info(msg)
                else:
                    # 4. Update the distro to point to the new origin
                    self.do_update_distro()
            except ClientError, ex:
                if ex.response['Error']['Code'] == 'AccessDenied':
                    msg = "usage: BOTO_CONFIG=<your credentials file path> python deploy_static.py"
                    logging.info(msg)    

    def deploy_application(self):
        
        if self.verbose or self.dry_run:
            logging.info("%s Deploying application for stack=[%s], product=[%s], template=[%s], template_url=[%s], params=[%s]" % (
                self.get_dry_run_str(),
                self.stack_name, self.product, self.template, self.template_url, self.parameters
            ))
        
        dest_bucket = self.get_app_bucket_name()
    
        if self.template:
            if not self.template_url:
                # Upload to S3
                if self.verbose or self.dry_run:
                    logging.info("%s Uploading template for stack=[%s], release_id=[%s], file=[%s]" % (
                        self.get_dry_run_str(),
                        self.stack_name,
                        self.release_id, self.template.name
                    ))
                
                if not self.dry_run:
                    root_template_url = self.upload_template(self.stack_name, template.name)
                    if root_template_url:
                        template_url = root_template_url
                    else:
                        logging.error("Problem uploading CloudFormation template: %s" % template.name)
                        return
            else:
                logging.info("%s Template url specified, not uploading: %s" % (
                    self.get_dry_run_str(),
                    template_url
                ))
        else:
            template_root_path_parts = self.deploy_configs['cfn-template-root-path']
            all_path_parts = template_root_path_parts + [self.deploy_configs['root-template-name']]
            
            #root_temp_path = os.path.join(".", "arm_app", "conf", "cfn", self.ROOT_TEMPLATE_NAME)
            root_temp_path = os.path.join(*all_path_parts)

            if self.verbose or self.dry_run:
                logging.info("%s Uploading template using default path for stack=[%s], release_id=[%s], file=[%s]" % (
                    self.get_dry_run_str(),
                    self.stack_name,
                    self.release_id,
                    root_temp_path
                ))

            if os.path.exists(root_temp_path):
                if not self.dry_run:
                    root_template_url = self.upload_template(root_temp_path)
            else:
                logging.error("Could not find template at %s" % root_temp_path)
        
        template_root_path_parts = self.deploy_configs['cfn-template-root-path']
        child_stack_template_urls = {}
        
        if 'nested-stack-templates' in self.deploy_configs:
            nested_stack_filenames = self.deploy_configs['nested-stack-templates']
            for nsf in nested_stack_filenames:
                nsf_path_parts = template_root_path_parts + [nsf]
                nsf_temp_path = os.path.join(*nsf_path_parts)
    
                if self.verbose or self.dry_run:
                    logging.info("%s Uploading nested template using default path for stack=[%s], release_id=[%s], file=[%s]" % (
                        self.get_dry_run_str(),
                        self.stack_name,
                        self.release_id,
                        nsf_temp_path
                    ))
    
                if not self.dry_run:
                    nsf_template_url = self.upload_template(nsf_temp_path)
                    child_stack_template_urls[nsf] = nsf_template_url
        
        """
        queue_temp_path = os.path.join(".", "arm_app", "conf", "cfn", self.QUEUE_TEMPLATE_NAME)

        if self.verbose or self.dry_run:
            logging.info("%s Uploading queue template using default path for stack=[%s], release_id=[%s], file=[%s]" % (
                self.get_dry_run_str(),
                self.stack_name,
                self.release_id,
                queue_temp_path
            ))
        
        if not self.dry_run:
            queue_template_url = self.upload_template(queue_temp_path)
        """
        
        if self.verbose or self.dry_run:
            logging.info("%s Validating templates" % self.get_dry_run_str())
        
        if not self.dry_run:
            ## Validate the template before we do anything else:
            cfn_client = boto3.client('cloudformation')
            try:
                resp1 = cfn_client.validate_template(TemplateURL=root_template_url)
            except:
                logging.exception("Problem validating template %s." % root_template_url)
                return
            
            for cst_name, cst_url in child_stack_template_urls.items():
                try:
                    resp2 = cfn_client.validate_template(TemplateURL=cst_url)
                except:
                    logging.exception("Problem validating template %s." % cst_url)
                    return

        if self.verbose or self.dry_run:
            logging.info("%s Building application" % (
                self.get_dry_run_str(),
            ))
            
        if self.verbose or self.dry_run:
            logging.info("%s Making database migrations" % (
                self.get_dry_run_str()
            ))
            
        ### Make any db migrations necessary
        (status, stdout, stderr) = self.deploy_lib.run_db_migrations()
        logging.info("%s db migration output: %s" % (self.get_dry_run_str(), stdout))
        if status != 0:
            logging.error("Problem making db migrations: %s" % stderr)
            return
        
        params = self.parameters or {}
        tarball = self.build()
        
        if self.verbose or self.dry_run:
            logging.info("%s Uplading application tarball" % (
                self.get_dry_run_str(),
            ))
        
        if not self.dry_run:
            url = self.upload_app(tarball)
        
        if self.verbose or self.dry_run:
            logging.info("%s Deploying application to CloudFormation" % (
                self.get_dry_run_str(),
            ))
        
        if not self.dry_run:
            temp_params = self.deploy_configs['template-parameter-names']

            params[temp_params['application-source-parameter-name']] = url
            params[temp_params['release-id-parameter-name']] = self.release_id

            ## TODO: Slurp a file of release notes or the git change log and put it here
            params[temp_params['release-notes-parameter-name']] = "No release notes"
            params[temp_params['root-stack-parameter-name']] = self.stack_name
            params[temp_params['app-bucket-parameter-name']] = dest_bucket
            params[temp_params['app-bucket-arn-param-name']] = "arn:aws:s3:::%s/*" % self.get_app_bucket_name()
            
            for nsf_fname, nsf_url in child_stack_template_urls.items():
                param_name = temp_params['nested-stack-param-name-dict'][nsf_fname]
                params[param_name] = nsf_url

            ### Add stack-specific vars
            for key, val_dict in self.stack_vars.items():
                val = val_dict['value']
                vtype = val_dict['type']
                if vtype in ['str', 'unicode']:
                    params[key] = "'%s'" % val
                else:
                    params[key] = "%s" % val
        
            self.cfndeploy(root_template_url, params)
            
    @classmethod
    def parameters_type(cls, arg):
        result = {}
        for nvp in arg.split(";"):
            n, _, v = nvp.partition("=")
            result[n] = v
        return result

if __name__ == "__main__":
    
    arg_parser = ArgumentParser("Deploy to AWS. Please be sure to use AWS_CONFIG_FILE=<file> for your credentials")
    arg_parser.add_argument("--config-path", default="./scripts/deploy-config.yaml",
                            help="Config for this specific company, project, and product.")
    arg_parser.add_argument("--static-src-root",
                            help="Source root folder that contains static content needing deployment.")
    arg_parser.add_argument("--update-distro", action='store_true',
                            default=False, help="Update the CloudFront distribution to point \
                                    to the new release folder")
    arg_parser.add_argument("--change-cloudfront-origin", action="store", help="Specify release ID to switch to. If this option is specified, other actions are ignored.")
    arg_parser.add_argument("--deploy-app", action="store_true", default=False, help="Deploy the app to AWS from the current branch.")
    arg_parser.add_argument("--no-static", action="store_true", default=False, help="Omit static deploy, typically if you need to just update the code.")

    arg_parser.add_argument("--template", type=FileType("r"))
    arg_parser.add_argument("--template-url")
    arg_parser.add_argument("--parameters", type=AppDeployer.parameters_type)
    
    ### TODO: Make a custom argparse Action that validates against a preset list of options for db migrator
    arg_parser.add_argument("--db-migrator", action="store", help="How to run db migrations.")
    arg_parser.add_argument("--no-db-migrations", action="store_true", default=False, help="Deploy without db migrations")
    arg_parser.add_argument("--product", help="Product name we are deploying. Defaults by using a PRODUCT file with a single string in it.")
    arg_parser.add_argument("--stamp", default=int(time.time()),
                            help="Time stamp as seconds since the epoch to use for generating release id")
    arg_parser.add_argument("--blessed", action='store_true',
                            default=False,
                            help="Generate a blessed release ID, which attempts \
                                 to leave off the compressed date stamp and commit id., \
                                 i.e., a release that looks like adaptrm-dev-aws-1.0.2 instead \
                                 of adaptrm-dev-aws-1.0.1-20160318T163105-60d00d7")
    arg_parser.add_argument("--dry-run", action="store_true", default=False)
    arg_parser.add_argument("--verbose", action="store_true", default=False)
    arg_parser.add_argument("stack_name")
    args = arg_parser.parse_args()
    
    if not args.config_path:
        print "--config-path is required. Please use deploy-config.yaml.sample as an example."
        exit(1)

    try:
        deployer = AppDeployer(**args.__dict__)
    except:
        logging.exception("Problem initializing AppDeployer.")
        exit(1)
    
    any_static = not deployer.no_static and any([deployer.upload_content, deployer.update_distro, deployer.revert_distro])
    any_app = deployer.deploy_app
    
    try:
        if any_static:
            deployer.deploy_static()

        if deployer.deploy_app:
            deployer.deploy_application()

    except ClientError, ex:
        print ex
        if ex.response['Error']['Code'] == 'AccessDenied':
            msg = "usage: BOTO_CONFIG=<your credentials file path> python deploy.py"
            logging.info(msg)
