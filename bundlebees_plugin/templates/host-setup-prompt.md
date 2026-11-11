# bundlebees worker host setup

You are helping set up a worker host for a Hermes bundlebees coordinator.

- Operating system: {{os}}
- Reach method: {{reach}}
- Model provider: {{provider}}
- Repositories:
{{repos}}
- Maximum concurrent workers: {{max_concurrent}}
- Branch rules: {{branch_rules}}

Use the host's documented secret store for the launcher token and provider
key. Refer to configuration paths and environment variable names only; never
print a token, key, bind address, private URL, or SSH host. Start the launcher
only from its documented path, then return a health summary that says whether
the token is configured (never its value). Do not restart a launcher that has
running work.
