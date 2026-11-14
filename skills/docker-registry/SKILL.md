---
name: docker-registry
description: Use and administer a private Docker registry built on Amazon ECR with token-based pulls, deployed from the chrcl/docker-repo CloudFormation stack. Use when pulling images from this registry, creating, rotating or revoking pull tokens for consumers, onboarding a GitHub repository so it can push images, listing or deleting images, or diagnosing docker login, pull or push errors against it.
compatibility: Admin tasks need the AWS CLI with credentials for the registry account. Pulls need Docker or another OCI client. Onboarding a repository needs the GitHub CLI (gh).
metadata:
  source: https://github.com/chrcl/docker-repo
---

# Docker registry on ECR with token pulls

## How the registry works

The registry has two addresses. Never mix them up.

| Use | Address | Credentials |
|---|---|---|
| Push | `<account>.dkr.ecr.<region>.amazonaws.com` (`EcrRegistry` output) | AWS: GitHub Actions OIDC role, or an administrator's credentials |
| Pull | Registry host (`RegistryHost` output), a custom domain | Registry user + token, no AWS credentials |

- Images live in ECR repositories named `<prefix>/<name>` (`RepositoryPrefix` output, usually `gh`). A repository is created automatically on its first push. Names must be lowercase.
- The pull side is read-only. Pushing through the registry host is refused.
- One consumer is one SSM `SecureString` parameter `<TokenParameterPath><user>`, for example `/docker-repo/tokens/alice`. Its value is either `TOKEN` (all repositories) or `TOKEN pattern1,pattern2` (only repositories matching the comma-separated patterns, `*` allowed, e.g. `TOKEN gh/app1,gh/team-*`).
- User names allow letters, digits, `_`, `.` and `-`.
- Token changes and revocations take effect after at most 60 seconds (proxy cache).

## Rules

- Never write a token, the AWS account ID or the registry domain into a file that is committed, an issue, a pull request, or a CI log. The registry source repository is public.
- Give a new token to the user once, in the conversation, and do not store it anywhere else.
- Pass tokens to `docker login` on standard input, never with `-p`.
- Ask for confirmation before deleting a repository, deleting images or revoking a token.
- Run each command on a single line.
- If the stack name or region is unknown, ask the user. The defaults are stack `docker-repo` and region `us-east-1`.

## 1. Read the registry configuration

```
aws cloudformation describe-stacks --region REGION --stack-name STACK --query "Stacks[0].Outputs" --output table
```

Outputs used below: `RegistryHost`, `EcrRegistry`, `RepositoryPrefix`, `TokenParameterPath`, `GitHubPushRoleArn`.

## 2. Pull an image (consumer)

Log in once. Docker stores the credentials.

```
printf '%s' "$TOKEN" | docker login REGISTRY_HOST -u USER --password-stdin
```

On Windows `cmd`, run `docker login REGISTRY_HOST -u USER` and paste the token at the password prompt: `echo TOKEN | ...` adds a trailing space and the token is rejected.

```
docker pull REGISTRY_HOST/PREFIX/NAME:TAG
```

Pulling by digest also works: `docker pull REGISTRY_HOST/PREFIX/NAME@sha256:DIGEST`.

Kubernetes pull secret:

```
kubectl create secret docker-registry registry-pull --docker-server=REGISTRY_HOST --docker-username=USER --docker-password="$TOKEN"
```

## 3. Create a consumer (admin)

Generate a token:

```
aws secretsmanager get-random-password --region REGION --password-length 48 --exclude-punctuation --query RandomPassword --output text
```

Store it, for all repositories:

```
aws ssm put-parameter --region REGION --name TOKEN_PATH/USER --type SecureString --value "TOKEN"
```

Or restricted to some repositories:

```
aws ssm put-parameter --region REGION --name TOKEN_PATH/USER --type SecureString --value "TOKEN gh/app1,gh/team-*"
```

Check that the token is accepted. Expected output: `{}`.

```
curl -s -u USER:TOKEN https://REGISTRY_HOST/v2/
```

## 4. List, rotate or revoke consumers (admin)

```
aws ssm get-parameters-by-path --region REGION --path TOKEN_PATH --query "Parameters[].Name" --output text
```

Rotate (generate a new token as in step 3 first):

```
aws ssm put-parameter --region REGION --name TOKEN_PATH/USER --type SecureString --value "NEW_TOKEN" --overwrite
```

Revoke:

```
aws ssm delete-parameter --region REGION --name TOKEN_PATH/USER
```

## 5. Onboard a GitHub repository to push images

The push role trusts the repositories of the GitHub account that owns the registry. A repository of another account cannot push.

1. Copy [assets/push-workflow.yml](assets/push-workflow.yml) to `.github/workflows/` in the target repository. Set `IMAGE_NAME` to `PREFIX/NAME` in lowercase, and adapt the trigger and build context if needed.
2. Create the `prod` environment and its variables in the target repository:

```
gh api --method PUT repos/OWNER/REPO/environments/prod
```
```
gh variable set AWS_REGION --env prod --repo OWNER/REPO --body "REGION"
```
```
gh variable set ECR_PUSH_ROLE_ARN --env prod --repo OWNER/REPO --body "PUSH_ROLE_ARN"
```

`PUSH_ROLE_ARN` is the `GitHubPushRoleArn` output. Keep `mask-aws-account-id: true` in the workflow: if the repository is public, its logs are public.

To push from a workstation instead, with administrator AWS credentials:

```
aws ecr get-login-password --region REGION | docker login --username AWS --password-stdin ECR_REGISTRY
```
```
docker push ECR_REGISTRY/PREFIX/NAME:TAG
```

## 6. List and delete images (admin)

```
aws ecr describe-repositories --region REGION --query "repositories[].repositoryName" --output text
```
```
aws ecr list-images --region REGION --repository-name PREFIX/NAME --query "imageIds[].imageTag" --output text
```
```
aws ecr batch-delete-image --region REGION --repository-name PREFIX/NAME --image-ids imageTag=TAG
```
```
aws ecr delete-repository --region REGION --repository-name PREFIX/NAME --force
```

Each repository already expires untagged images and keeps a bounded number of images (lifecycle policy set at creation).

## 7. Troubleshooting

See [references/troubleshooting.md](references/troubleshooting.md) for the meaning of each error and its fix.
