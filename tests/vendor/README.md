# Plotly.js test fixture

`plotly-2.35.2.min.js` is the minified browser bundle from the npm package
`plotly.js-dist-min@2.35.2`. The application pins this same version in
`ngapp.components.visualization.PlotlyComponent._SRC`.

The Playwright fixture serves this file for that exact CDN URL so GUI tests do
not depend on a third-party network request. Production application loading is
unchanged.

Source package: <https://registry.npmjs.org/plotly.js-dist-min/-/plotly.js-dist-min-2.35.2.tgz>

The package tarball SHA-1 is `0d08bcc5dac84c44c36eccdfffdcfd2a6cbefe11`.
The bundle SHA-256 is
`6d21266ce1bd7d9e5ab4e115989c70c20de0382fd973a8f26ab58619eba4d603`.

The accompanying `plotly-2.35.2-LICENSE.txt` contains the package's MIT
license.
