#! /bin/bash

export AWS_ACCESS_KEY=`curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/ducttape-master | grep AccessKeyId | awk -F\" '{ print $4 }'`
export AWS_SECRET_KEY=`curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/ducttape-master | grep SecretAccessKey | awk -F\" '{ print $4 }'`
export AWS_SESSION_TOKEN=`curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/ducttape-master | grep Token | awk -F\" '{ print $4 }'`
