"""What build this is.

Overwritten by CI immediately before freezing; the values below are what a run from
source reports. It exists because the first Windows install of the cloud work came back
as "I don't see any of it", and there was no way for either of us to tell whether the
wrong build was installed, the installer had skipped a running app, or the browser was
serving a cached UI. Three very different problems, indistinguishable without this.
"""
COMMIT = "source"
BUILD = "development"
BUILT = ""
