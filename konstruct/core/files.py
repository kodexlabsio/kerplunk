"""The names of the files Konstruct keeps beside a project's own.

They live apart from the modules that read the files, which load the SQL parser: the hook's
fingerprint needs to know whether one changed, after every edit, and nothing more.
"""

LAWS = 'laws.json'
"""The data laws, in the output directory (:mod:`konstruct.analysis.laws`)."""

LOCK = 'konstruct.lock'
"""The lineage lockfile, at a project's root (:mod:`konstruct.analysis.lock`)."""
