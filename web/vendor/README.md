# Vendored third-party assets

## scalar.standalone.js

[Scalar API Reference](https://github.com/scalar/scalar), version 1.66.1, MIT licensed.
Copyright (c) Scalar contributors.

Vendored rather than loaded from a CDN. The docs page runs on the same origin as
the authenticated console, so a script there can make credentialed same-origin
requests. A CDN copy can change under us at any time; this one cannot change
without a commit and a review.

To update:

    curl -sSL -o web/vendor/scalar.standalone.js \
      https://cdn.jsdelivr.net/npm/@scalar/api-reference@<version>/dist/browser/standalone.js

Then bump the version here and re-run the suite.
