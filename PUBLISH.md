# Publishing to GitHub (one-time)

```bash
cd kestrel                                  # this folder
git init -b main
git add -A
git commit -m "Kestrel File Browser 1.0"
gh repo create kestrel --public --source . --push     # or create the repo on github.com and:
# git remote add origin git@github.com:<you>/kestrel.git && git push -u origin main

# first release: attaches dist/kestrel-1.0-portable.zip and kestrel.py automatically
git tag v1.0 && git push --tags
```

Before pushing, replace `<you>` in README.md and CONTRIBUTING.md with your GitHub user name
(`sed -i 's/<you>/YOURNAME/g' README.md CONTRIBUTING.md`) and check the copyright line in LICENSE.

CI (`.github/workflows/ci.yml`) runs on every push: rebuilds `kestrel.py` from `src/` and fails if
the committed file is out of date, checks Python 3.9 syntax, runs the headless self-test and builds
the portable zip. Tagging `v*` publishes a GitHub Release with the zip.

Delete this file once you've published.
