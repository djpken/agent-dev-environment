# CI/CD for the Agent Development Environment VM

GitHub Actions runs the Python unit tests and builds the web service for pull
requests and pushes to `main`. When a push to `main` passes, the workflow moves
the `deploy` branch to that tested commit. The VM polls the public `deploy`
branch once per minute, builds a plain release directory, switches the service
to it, and checks `http://127.0.0.1:6790/`. It restores the previous source and
restarts the service if the health check fails.

The VM does not run a GitHub Actions runner and needs no GitHub deploy secret.
The release is extracted with `git archive`, so Orca worktree cleanup cannot
remove the active deployment.

## Enable on an existing VM

After the workflow and `deploy` branch exist, run the installer once from a
checkout on the VM:

```bash
sudo ./deploy/agent-environment/agent-environment-cd-install
```

Check the polling timer and deployment logs with:

```bash
systemctl status agent-environment-cd.timer
journalctl -u agent-environment-cd.service -n 100 --no-pager
```

The deployment adapter currently builds and deploys the Node service used by
the `main` branch. A future Go runtime migration needs a matching deployment
adapter and Go toolchain on the VM before that runtime is promoted for deploy.
