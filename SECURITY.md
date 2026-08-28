# Security Policy

## Supported version

Security fixes are applied to the latest version on the default branch.

## Deployment boundary

AI HOT Review is a local single-user tool. It has no authentication or
multi-tenant authorization layer and binds to `127.0.0.1` by default. Do not
expose it directly to an untrusted network. If remote access is required, put
it behind an authenticated TLS reverse proxy and restrict access to the data
directory.

The service writes append-only review and view events. Those files may reveal
research interests and should be treated as private data.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not include tokens, private data files, or full production logs in a public
issue.
