# IXO

## Customizations

All customization tags MUST map to the upstream tag they are based on.
All ixo tags MUST be prefixed with `ixo` and MUST be suffixed with own version counter `.1` since we can have multiple versions of ixo customizations per upstream release. (eg: `v1.119.0-ixo.1`)
Please list changes below based of which ixo tag version the customization will be applied.

### Changelog

#### v1.113.0-ixo.1

- Increasing API `MAX_PDU_SIZE` to 10MB for handling of ai-oracles events.

## Contributing

Below are the steps to contribute to Ixo Synapse with your own custom configurations, or to update the existing code with latest upstream changes.

```bash
# 1. Add the upstream remote (only need to do this once)
git remote add upstream https://github.com/element-hq/synapse.git

# The below 4 steps is to keep forked repo in sync with upstream
# 2. Fetch only the develop branch and all tags from upstream
git fetch upstream develop --tags
# 3. Check out your develop branch
git checkout develop
# 4. Merge the upstream develop branch
git merge upstream/develop
# 5. Push the updated develop branch to your fork with new tags
git push origin develop --follow-tags

# 6. Check out your custom branch
git checkout ixo

# 7. Merge a specific version (tag) into your custom branch
git merge v1.113.0  # Replace with the specific version tag you want to merge

# 8. Make customizations(if have any) and commit
git add .
git commit -m "feat: add customizations"

# 8. Resolve any conflicts if they occur

# 9. Run tests and make sure everything works with your customizations

# 10. Create a new tag for your customized version
git tag -a v1.113.0-ixo -m "Synapse v1.113.0 with IXO customizations"

# 11. Push ixo custom branch and the new tags
git push origin ixo --follow-tags
# git push origin ixo
# git push origin 1.119.0-ixo

# 12. Build and push your Docker image
DOCKER_BUILDKIT=1 docker build --platform=linux/amd64 -t ghcr.io/ixoworld/synapse:v1.113.0-ixo.1 -f docker/Dockerfile .
docker push ghcr.io/ixoworld/synapse:v1.113.0-ixo.1
```

# Resources

- [Synapse](https://github.com/element-hq/synapse)
- [Ixo Synapse](https://github.com/ixoworld/synapse/tree/ixo)
- [Contribution Guidelines](https://element-hq.github.io/synapse/latest/development/contributing_guide.html)
