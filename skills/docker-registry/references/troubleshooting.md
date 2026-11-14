# Troubleshooting

## Pull side (registry host)

| Symptom | Cause | Fix |
|---|---|---|
| `curl -i https://REGISTRY_HOST/v2/` gives `401` with `www-authenticate: Basic realm="docker-registry"` | Normal: the registry asks for credentials | None |
| `docker login` fails, or `401` with `UNAUTHORIZED` | Unknown user, wrong token, or token created less than 60 seconds ago | Check the parameter exists under the token path; wait 60 seconds after a change |
| `403` with `DENIED` / `token not allowed for this repository` | The token is restricted to other repositories | Add the repository pattern to the parameter value |
| `404` with `MANIFEST_UNKNOWN` or `NAME_UNKNOWN` | Tag or repository does not exist, or the prefix is missing | Check the name with `aws ecr list-images`; image names are `PREFIX/NAME` |
| `405 Method Not Allowed` on `HEAD .../manifests/...` | Normal: the proxy refuses `HEAD` on manifests, Docker and containerd retry with `GET` | None. A tool that only uses `HEAD` (for example to compare digests) is not supported |
| `unable to fetch descriptor ... which reports content size of zero` | The deployed proxy predates the `405` fix | Redeploy the registry stack from the current `template.yaml` |
| `502` with `registry backend error` | The proxy could not read ECR, or the manifest is larger than 4 MB | Retry; check the proxy Lambda logs in CloudWatch |
| `403` on `POST`, `PUT` or `PATCH` | Normal: the registry host is read-only | Push to the ECR address instead |
| Layer download fails after the manifest succeeds | The client cannot reach the AWS S3 endpoints of the region (layers are downloaded from S3 directly) | Allow outbound HTTPS to S3 for that region |

## Push side (ECR)

| Symptom | Cause | Fix |
|---|---|---|
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` in GitHub Actions | The repository does not belong to the GitHub account trusted by the push role, or the workflow lacks `id-token: write` | Push from a repository of the registry owner's account; keep the `permissions` block of the workflow |
| `The web identity token provided could not be validated` | The account's GitHub OIDC provider is missing or misconfigured | The provider URL must be `token.actions.githubusercontent.com` with audience `sts.amazonaws.com` |
| `AccessDenied` on push, or `RepositoryNotFoundException` | The image name is outside the registry prefix | Name the image `PREFIX/NAME`, lowercase |
| `denied: Your authorization token has expired` | ECR login is valid 12 hours | Run `aws ecr get-login-password ... | docker login ...` again |

## Useful checks

Token parameter exists:

```
aws ssm get-parameter --region REGION --name TOKEN_PATH/USER --query "Parameter.Name" --output text
```

Images of a repository:

```
aws ecr describe-images --region REGION --repository-name PREFIX/NAME --query "imageDetails[].{Tags:imageTags,Digest:imageDigest,Pushed:imagePushedAt}"
```
