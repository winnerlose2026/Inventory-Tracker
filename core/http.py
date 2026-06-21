"""Outbound HTTP safety helpers shared across routes/blueprints."""

# Hosts the app is ever allowed to make outbound requests to — used to defeat
# partial-SSRF where a user-influenced value lands in a request URL.
_TRUSTED_OUTBOUND_HOSTS = frozenset({
    "graph.microsoft.com",
    "login.microsoftonline.com",
})
