#!/bin/bash -e

while [ $# -gt 0 ]; do
  OPTION=$1
  case $OPTION in
    --update)
      UPDATE="yes"
      shift
      ;;
    *)
      break
      ;;
  esac
done


if [ ! -e kafka ]; then
    echo "Cloning Kafka"
    git clone http://git-wip-us.apache.org/repos/asf/kafka.git kafka
fi

pushd kafka

if [ "x$UPDATE" == "xyes" ]; then
    echo "Updating Kafka"
    git pull origin
fi

git checkout 0.8.2-beta

# FIXME we should be installing the version of Kafka we built into the local
# Maven repository and making sure we specify the right Kafka version when
# building our own projects. Currently ours link to whatever version of Kafka
# they default to, which should work ok for now.
echo "Building Kafka"
KAFKA_BUILD_OPTS=""
if [ "x$SCALA_VERSION" != "x" ]; then
    KAFKA_BUILD_OPTS="$KAFKA_BUILD_OPTS -PscalaVersion=$SCALA_VERSION"
fi
if [ ! -e gradle/wrapper/ ]; then
    gradle
fi
./gradlew $KAFKA_BUILD_OPTS jar
popd

function build_maven_project() {
    NAME=$1
    URL=$2
    # The build target can be specified so that shared libs get installed and
    # can be used in the build process of applications, but applications only
    # need to build enough to be tested.
    BUILD_TARGET=$3

    if [ ! -e $NAME ]; then
        echo "Cloning $NAME"
        git clone $URL $NAME
    fi

    # Turn off tests for the build because some of these are local integration
    # tests that take a long time. This shouldn't be a problem since these
    # should be getting run elsewhere.
    BUILD_OPTS="-DskipTests"
    if [ "x$SCALA_VERSION" != "x" ]; then
        BUILD_OPTS="$BUILD_OPTS -Dkafka.scala.version=$SCALA_VERSION"
    fi

    pushd $NAME

    if [ "x$UPDATE" == "xyes" ]; then
        echo "Updating $NAME"
        git pull origin
    fi

    echo "Building $NAME"
    mvn $BUILD_OPTS $BUILD_TARGET
    popd
}

build_maven_project "common" "git@github.com:confluentinc/common.git" "install"
build_maven_project "rest-utils" "git@github.com:confluentinc/rest-utils.git" "install"
build_maven_project "kafka-rest" "git@github.com:confluentinc/kafka-rest.git" "package"