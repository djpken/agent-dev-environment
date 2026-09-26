# ADES local commit deployment

ADES checks the configured local Git repository's `HEAD` every five seconds.
When a commit changes `HEAD`, the service queues one deployment attempt for
that SHA. It does not poll GitHub, and pushes alone do not deploy unless the
local repository checks out the pushed commit. The worker builds the exact
commit with `git archive`, switches the service after a successful build, and
restores the previous runtime if its health check fails. A failed SHA is not
retried.

Install the local deployment worker on the VM once:

```bash
sudo ./deploy/agent-environment/agent-environment-local-deploy-install \
  --repo-root /home/orca/projects/agent-dev-environment
```

The ADES dashboard lists the latest commit, deployment stage, timestamps, and
failure reason under **本機部署**. It updates every four seconds while a deploy
is running. If ADES cannot queue a deployment, inspect the dashboard and local
systemd journal.

The VM builds committed source locally. Pushing remains a separate Git command.
