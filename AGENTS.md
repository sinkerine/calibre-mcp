# Repository verification

Sandbox end-to-end testing is not needed for this repository, as explicitly decided by the user.

Existing unit tests must still run for changes, future pushes, and deployments:

```sh
nix develop -c python -m unittest discover -s tests -v
```

For repositories that require sandbox E2E, this workflow runs it only when a feature is first added, including necessary repairs and reruns; future pushes and deployments of an existing feature do not require rerunning those sandbox tests. This repository is exempt from sandbox E2E testing. The exemption does not waive existing unit tests.
