## Contributing

Contributions are accepted via pull request and will be reviewed as soon as possible. If you have access to master, please do not commit directly! Pull requests only!

Code style should follow PEP-8 with a loose line length of 100 characters (don't make the code ugly).

### Pull request requirements

For pull request to be merged, following requirements should be met:

- Tests covering new or changed code are added or updated
- Relevant documentation should be updated or added
- Line item should be added to CHANGELOG.md, unless change is really irrelevant

## Testing

For running the tests, (the first time only) you just need to run:

    $ ./runtests.sh

This will download the App Engine SDK, pip install a bunch of stuff locally, download the Django tests and run them. If you want to run the
tests on a specific Django version, simply do:

    $ DJANGO_VERSION=1.8 ./runtests.sh

Currently the default is 1.7. TravisCI runs on 1.7 and 1.8 currently, and 1.9 in the 1-9-support branch.

After you have run the tests once, you can do:

    $ cd testapp
    ./runtests.sh

This will avoid the re-downloading of the SDK and libraries.  Note that if you want to switch Django version then you need to use the `runtests.sh` command in the parent directory again.

You can run specific tests in the usual way by doing:

    ./runtests.sh some_app.SomeTestCase.some_test_method
