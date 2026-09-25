# Local commit deployment

GitHub Actions runs CI checks only. It does not promote a `deploy` branch, and
the VM does not poll GitHub for releases. A local `post-commit` hook queues one
deployment attempt for the committed SHA. The worker builds the exact commit
with `git archive`, switches the service after a successful build, and restores
the previous runtime if its health check fails. A failed SHA is not retried.

Install the hook and local worker on the VM once:

```bash
sudo ./deploy/agent-environment/agent-environment-local-deploy-install \
  --repo-root /home/orca/projects/agent-dev-environment
```

The ADES dashboard lists the latest commit, deployment stage, timestamps, and
failure reason under **本機部署**. It updates every four seconds while a deploy
is running. The hook leaves the commit successful if the deployment cannot be
queued; inspect the dashboard and local systemd journal for that failure.

The VM builds committed source locally. Pushing remains a separate Git command;
the GitHub workflow verifies pushes and pull requests but cannot deploy them.
