# Test services to run the sdk-test-suite

## To run locally

* Grab the release of sdk-test-suite: https://github.com/restatedev/sdk-test-suite/releases

* Prepare the docker image: 
```shell
docker build . -f test-services/Dockerfile -t restatedev/test-services
```

* Run the tests (requires JVM >= 17):
```shell
java -jar restate-sdk-test-suite.jar run --exclusions-file test-services/exclusions.yaml restatedev/test-services
```

## To debug a single test:

* Run the python service using your IDE
* Run the test runner in debug mode specifying test suite and test:
```shell
java -jar restate-sdk-test-suite.jar debug --test-suite=lazyState --test-name=dev.restate.sdktesting.tests.State default-service=9080
```

For more info: https://github.com/restatedev/sdk-test-suite