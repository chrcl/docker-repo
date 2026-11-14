# docker-repo

Pile CloudFormation qui héberge des images Docker privées sur AWS (ECR) :

- **push** depuis GitHub Actions par OIDC, sans aucune clé AWS stockée dans GitHub ;
- **pull** par les consommateurs avec un simple `docker login` utilisateur + token, sans aucun identifiant AWS ;
- un token par consommateur, révocable, éventuellement limité à certains dépôts.

```
GitHub Actions --(OIDC, rôle IAM temporaire)--> ECR  <prefixe>/<image>
                                                 ^
Consommateur --docker login/pull--> CloudFront --+-- Lambda (vérifie le token, lit ECR)
     |                                           
     +------ couches téléchargées en direct depuis S3 (URL pré-signée fournie par ECR)
```

## Pourquoi cette architecture

- ECR n'accepte nativement que des identifiants AWS (IAM) : le mot de passe de `docker login` est un jeton dérivé d'IAM valable 12 h. Il n'existe pas de token statique ECR.
- Docker ne sait pas s'authentifier auprès d'un registre par clé SSH. Les mécanismes natifs sont l'authentification Basic (utilisateur + mot de passe) et le jeton Bearer. Cette pile utilise Basic, pris en charge par Docker, containerd, Podman et les `imagePullSecrets` de Kubernetes.
- Un petit proxy fait donc le lien : il vérifie le token du consommateur puis interroge ECR avec son propre rôle IAM. Il ne transporte que les manifestes ; les couches sont téléchargées directement depuis S3.
- CloudFront est nécessaire devant la Lambda : Docker n'envoie ses identifiants qu'après une réponse 401 portant l'en-tête `WWW-Authenticate`, et une URL de fonction Lambda renomme cet en-tête. Le 401 est donc émis par une fonction CloudFront.

## Déploiement

Les piles sont déployées par GitHub Actions avec AWS SAM. Les workflows assument des rôles IAM par OIDC : aucune clé AWS n'est stockée dans GitHub.

| Événement | Workflow | Effet |
|---|---|---|
| Push sur `main` modifiant `template.yaml`, ou lancement manuel | `deploy.yml` | Déploie la pile principale `STACK_NAME` |
| Pull request modifiant `template.yaml` (ouverture, nouveaux commits) | `deploy.yml` | Déploie une pile de test `STACK_NAME-pr<numéro>` |
| Fermeture d'une pull request, fusionnée ou non | `cleanup-pr.yml` | Supprime la pile `STACK_NAME-pr<numéro>` |

Une pile de pull request est une copie complète et indépendante : son propre registre (distribution CloudFront et nom DNS distincts), son propre préfixe de dépôts ECR `gh-pr<numéro>`, son propre chemin de tokens `/STACK_NAME-pr<numéro>/tokens/`. Les pull requests venant d'un fork ne sont jamais déployées.

### Préparation, une seule fois, par un administrateur AWS

**Prérequis créés en dehors de ce dépôt :**

- le fournisseur OIDC `token.actions.githubusercontent.com` dans le compte AWS ;
- une zone Route 53 pour le domaine du registre ;
- un certificat ACM valide, dans la région `us-east-1` (exigence de CloudFront), couvrant les noms du registre (voir « Nom du registre » ci-dessous) ;
- un bucket S3 dans la région du registre, où SAM téléverse le template.

**1. Les paramètres du compte** (`template-account.yaml`). Cette pile ne crée aucune ressource : elle exporte le nom de la zone, son identifiant et l'ARN du certificat, que les autres templates lisent avec `Fn::ImportValue`. Elle se déploie dans la région du registre, car un export n'est visible que dans sa région.

```
aws cloudformation deploy --template-file template-account.yaml --stack-name docker-registry-account --region MA_REGION --parameter-overrides ZoneName=docker.example.com HostedZoneId=ZONE_ID CertificateArn=ARN_DU_CERTIFICAT
```

Un export utilisé par une autre pile ne peut être ni modifié ni supprimé : pour changer de zone ou de certificat, il faut d'abord supprimer les piles du registre (principale et pull requests) et celle des rôles.

**2. Les rôles de déploiement et de suppression** (`template-role.yaml`), dans la même région, après l'étape 1 :

```
aws cloudformation deploy --template-file template-role.yaml --stack-name github-deploy-docker-repo --region MA_REGION --capabilities CAPABILITY_IAM --parameter-overrides GitHubOwner=MON_COMPTE_GITHUB GitHubRepo=docker-repo StackName=docker-repo AssetsBucketName=MON_BUCKET
```

Récupérer les ARN des deux rôles :

```
aws cloudformation describe-stacks --stack-name github-deploy-docker-repo --region MA_REGION --query "Stacks[0].Outputs" --output table
```

Contraintes de nommage : le nom de la pile des rôles ne doit pas commencer par `<StackName>-` (ici `docker-repo-`), sinon les rôles pourraient se modifier eux-mêmes. Aucune autre pile du compte ne doit s'appeler `<StackName>-pr...` : elle serait couverte par les droits des deux rôles.

### Nom du registre

Le registre répond toujours sur un nom de la zone exportée, et la pile y crée elle-même ses enregistrements DNS (alias A et AAAA vers CloudFront). Le nom dépend de la variable GitHub optionnelle `REGISTRY_SUBDOMAIN` :

| `REGISTRY_SUBDOMAIN` | Pile principale | Pile de la pull request 12 | Le certificat doit couvrir |
|---|---|---|---|
| non définie | `<zone>` | `pr12.<zone>` | `<zone>` et `*.<zone>` |
| `docker` | `docker.<zone>` | `docker-pr12.<zone>` | `*.<zone>` |

Sans `REGISTRY_SUBDOMAIN`, le registre prend le nom de la zone elle-même : à réserver à une zone dédiée au registre. Si la zone est celle d'un domaine qui sert à autre chose, définir `REGISTRY_SUBDOMAIN`.

### Configuration de l'environnement GitHub `prod`

Les workflows lisent leurs secrets et leurs variables dans un environnement GitHub nommé `prod`, et non dans les secrets et variables du dépôt. Les deux jobs déclarent `environment: prod`, y compris pour les piles de pull request.

| Type | Nom | Valeur |
|---|---|---|
| Secret | `AWS_DEPLOY_ROLE_ARN` | Sortie `DeployRoleArn` |
| Secret | `AWS_DELETE_ROLE_ARN` | Sortie `DeleteRoleArn` |
| Secret | `SAM_ASSETS_BUCKET` | Nom du bucket S3 (secret pour ne pas apparaître dans les journaux) |
| Variable | `AWS_REGION` | Région du registre |
| Variable | `STACK_NAME` | Nom de la pile principale, identique au paramètre `StackName` des rôles (ici `docker-repo`, 20 caractères au plus) |
| Variable (optionnelle) | `REGISTRY_SUBDOMAIN` | Étiquette DNS du registre dans la zone (voir ci-dessus) |

Avec la CLI GitHub (`gh auth login` au préalable). Créer l'environnement :

```
gh api --method PUT repos/MON_COMPTE_GITHUB/docker-repo/environments/prod
```

Les secrets :

```
gh secret set AWS_DEPLOY_ROLE_ARN --env prod --repo MON_COMPTE_GITHUB/docker-repo --body "ARN_DU_ROLE_DE_DEPLOIEMENT"
```
```
gh secret set AWS_DELETE_ROLE_ARN --env prod --repo MON_COMPTE_GITHUB/docker-repo --body "ARN_DU_ROLE_DE_SUPPRESSION"
```
```
gh secret set SAM_ASSETS_BUCKET --env prod --repo MON_COMPTE_GITHUB/docker-repo --body "MON_BUCKET"
```

Les variables :

```
gh variable set AWS_REGION --env prod --repo MON_COMPTE_GITHUB/docker-repo --body "MA_REGION"
```
```
gh variable set STACK_NAME --env prod --repo MON_COMPTE_GITHUB/docker-repo --body "docker-repo"
```

La variable optionnelle, si le registre ne doit pas prendre le nom de la zone elle-même :

```
gh variable set REGISTRY_SUBDOMAIN --env prod --repo MON_COMPTE_GITHUB/docker-repo --body "docker"
```

Vérifier :

```
gh secret list --env prod --repo MON_COMPTE_GITHUB/docker-repo
```
```
gh variable list --env prod --repo MON_COMPTE_GITHUB/docker-repo
```

Si `SAM_ASSETS_BUCKET` a déjà été créé comme variable, le supprimer après avoir créé le secret :

```
gh variable delete SAM_ASSETS_BUCKET --env prod --repo MON_COMPTE_GITHUB/docker-repo
```

### Dépôt public : ce qui reste privé

Le dépôt ne contient ni numéro de compte AWS, ni domaine, ni identifiant de zone, ni ARN réel : uniquement des exemples (`example.com`, `MON_BUCKET`...). Les valeurs réelles vivent dans l'environnement `prod` et dans la pile `template-account.yaml`.

Les journaux des workflows d'un dépôt public sont publics. Les workflows y protègent les valeurs sensibles :

| Valeur | Protection |
|---|---|
| Numéro de compte AWS | Masqué par `configure-aws-credentials` (`mask-aws-account-id`) et par une étape dédiée, juste après l'obtention des identifiants |
| Nom de la zone, donc du registre | Masqué par la même étape, qui lit l'export `DockerRegistry-ZoneName` |
| ARN des rôles, nom du bucket | Secrets, masqués automatiquement par GitHub |
| Nom CloudFront de la distribution | N'est plus une sortie de la pile, donc jamais affiché |

Une valeur masquée apparaît `***` partout dans le journal, y compris à l'intérieur d'une autre chaîne (ARN, nom d'hôte). Restent visibles : la région, le nom des piles, l'étiquette `REGISTRY_SUBDOMAIN` et le compte GitHub, déjà public.

Deux limites :

- Les sorties de la pile ne sont plus affichées par le workflow : les lire avec la commande `aws cloudformation describe-stacks` de la section « Déploiement manuel ».
- Le masquage ne couvre que les workflows de ce dépôt. Un dépôt de projet public qui pousse des images doit garder `mask-aws-account-id: true` (déjà présent dans `examples/github-workflow.yml`).

Points à connaître sur l'environnement :

- **Disponibilité.** D'après la documentation GitHub, les secrets d'environnement ne sont disponibles sur un dépôt privé qu'à partir de l'offre GitHub Pro ; avec GitHub Free, ils sont réservés aux dépôts publics. À vérifier pour votre compte avant de compter dessus.
- **Ne pas restreindre les branches autorisées** de l'environnement : les jobs de pull request seraient refusés et les piles de test ne se déploieraient plus.
- **Relecteurs obligatoires.** Si l'offre GitHub le permet, exiger une approbation sur l'environnement soumet chaque déploiement et chaque suppression à validation. C'est le seul garde-fou devant les rôles AWS (voir ci-dessous).

Le workflow passe à la pile le propriétaire de ce dépôt GitHub (`GitHubOwner` et `GitHubOwnerId`) : ce sont les dépôts de ce compte qui pourront pousser des images. Les autres paramètres gardent leur valeur par défaut.

### Ce que les rôles peuvent faire

Les deux rôles sont assumables par tout job du dépôt qui déclare l'environnement `prod` (paramètre `GitHubEnvironment` de `template-role.yaml`), que le job vienne de `main` ou d'une pull request.

Rôle de déploiement :

- Créer et mettre à jour les piles `STACK_NAME` et `STACK_NAME-pr*`, et écrire dans le bucket des artefacts.
- Créer et modifier les rôles IAM, la fonction Lambda et le groupe de journaux dont le nom commence par `<STACK_NAME>-`.
- Gérer les distributions, fonctions et politiques CloudFront du compte, ainsi que les modèles de création de dépôts ECR : ces services ne permettent pas de restreindre les droits aux seules ressources des piles.
- Supprimer une pile `STACK_NAME-pr*`, pour repartir d'une première création échouée. Il ne peut pas supprimer la pile principale.

Rôle de suppression :

- Supprimer les piles `STACK_NAME-pr*` et leurs ressources. Il ne peut rien créer.
- Supprimer ou désactiver n'importe quelle distribution CloudFront du compte, pour la même raison que ci-dessus : c'est le risque principal de ce rôle.

Deux conséquences à connaître :

- **Toute personne pouvant pousser une branche dans le dépôt et ouvrir une pull request peut assumer les deux rôles** : il lui suffit d'un workflow déclarant `environment: prod`, qu'elle peut ajouter ou modifier dans sa pull request. Sans relecteurs obligatoires sur l'environnement, cela se fait sans validation. Comme le rôle de déploiement peut créer des rôles IAM `<STACK_NAME>-*` avec la politique de son choix, cela revient à des droits étendus sur le compte AWS. L'accès en écriture au dépôt est donc à réserver aux personnes de confiance.
- Si la toute première création de la pile principale échoue, elle reste en état `ROLLBACK_COMPLETE` et doit être supprimée par un administrateur avant de relancer le workflow (commande en fin de document). Pour une pile de pull request, le workflow le fait lui-même.

### Ce que la suppression d'une pile de pull request laisse en place

Les dépôts ECR `gh-pr<numéro>/*` créés par un push de test et les tokens SSM créés à la main ne font pas partie de la pile. Ils restent, et le stockage ECR reste facturé, jusqu'à suppression manuelle :

```
aws ecr delete-repository --repository-name gh-pr12/mon-image --force
```
```
aws ssm delete-parameter --name /docker-repo-pr12/tokens/alice
```

### Déploiement manuel

Le template reste du CloudFormation YAML standard, sans transformation SAM ni fichier à empaqueter. Il importe les exports de `template-account.yaml`, qui doit être déployé avant dans la même région :

```
aws cloudformation deploy --template-file template.yaml --stack-name docker-repo --capabilities CAPABILITY_IAM --parameter-overrides GitHubOwner=MON_COMPTE_GITHUB GitHubOwnerId=ID_NUMERIQUE
```

`ID_NUMERIQUE` est le champ `id` renvoyé par :

```
curl -s https://api.github.com/users/MON_COMPTE_GITHUB
```

La création de la distribution CloudFront prend plusieurs minutes. Afficher les sorties de la pile (hôte du registre, rôle de push, chemin des tokens) :

```
aws cloudformation describe-stacks --stack-name docker-repo --query "Stacks[0].Outputs" --output table
```

### Paramètres de la pile

| Paramètre | Défaut | Rôle |
|---|---|---|
| `GitHubOwner` | (obligatoire) | Compte ou organisation GitHub autorisé à pousser |
| `GitHubOwnerId` | `*` | Identifiant numérique du compte, pour le nouveau format de jeton OIDC |
| `GitHubRepo` | `*` | Dépôt GitHub autorisé (`*` = tous ceux du compte) |
| `RepositoryPrefix` | `gh` | Préfixe des dépôts ECR ; les images s'appellent `gh/<nom>` |
| `ImageTagMutability` | `MUTABLE` | `IMMUTABLE` interdit de repousser un tag existant |
| `UntaggedRetentionDays` | `7` | Suppression des images sans tag après N jours |
| `MaxImagesPerRepository` | `50` | Nombre maximal d'images par dépôt |
| `TokenCacheSeconds` | `60` | Cache des tokens, donc délai maximal d'une révocation |
| `SubDomain` | vide | Étiquette DNS du registre dans la zone exportée ; vide = le nom de la zone |

La zone Route 53 et le certificat ne sont pas des paramètres : la pile les importe depuis les exports de `template-account.yaml`, qui doit donc être déployé avant, dans la même région.

## Déclarer un projet et un client

Le registre accueille plusieurs projets : un dépôt ECR `gh/<projet>` par dépôt GitHub qui publie une image.

**Nouveau projet (push).** Rien à déclarer côté AWS :

1. Copier `examples/github-workflow.yml` dans `.github/workflows/` du dépôt GitHub et y régler `IMAGE_NAME` sur `gh/<projet>`.
2. Créer dans ce dépôt l'environnement GitHub `prod` avec les variables `AWS_REGION` et `ECR_PUSH_ROLE_ARN` (sortie `GitHubPushRoleArn` de la pile) :

```
gh api --method PUT repos/MON_COMPTE_GITHUB/MON_PROJET/environments/prod
```
```
gh variable set AWS_REGION --env prod --repo MON_COMPTE_GITHUB/MON_PROJET --body "MA_REGION"
```
```
gh variable set ECR_PUSH_ROLE_ARN --env prod --repo MON_COMPTE_GITHUB/MON_PROJET --body "ARN_DU_ROLE_DE_PUSH"
```

Le dépôt ECR est créé au premier push. Tous les dépôts GitHub du compte partagent le même rôle de push : chacun peut pousser dans n'importe quel `gh/*`, il n'y a pas de cloisonnement entre projets à l'écriture.

**Nouveau client (pull).** Un client est un couple utilisateur + token, déclaré par un paramètre SSM. Deux commandes, détaillées dans la section suivante : générer un token, puis créer le paramètre `/docker-repo/tokens/<utilisateur>`. Le client fait ensuite `docker login` une fois ; Docker mémorise l'identifiant.

Deux usages typiques :

- un client pour vous-même, sans restriction, qui tire toutes les images ;
- un client par consommateur externe, limité à un projet avec la valeur `"TOKEN gh/<projet>"`.

## Gérer les tokens de pull

Un consommateur = un paramètre SSM `SecureString` nommé `/<pile>/tokens/<utilisateur>`. Le nom d'utilisateur accepte lettres, chiffres, `_`, `.` et `-`.

Générer un token aléatoire :

```
aws secretsmanager get-random-password --password-length 48 --exclude-punctuation --query RandomPassword --output text
```

Créer le consommateur `alice` avec accès à toutes les images :

```
aws ssm put-parameter --name /docker-repo/tokens/alice --type SecureString --value "TOKEN"
```

Créer le consommateur `bob` limité à certains dépôts (motifs séparés par des virgules, `*` accepté) :

```
aws ssm put-parameter --name /docker-repo/tokens/bob --type SecureString --value "TOKEN gh/app1,gh/equipe-*"
```

Changer un token (ajouter `--overwrite`), lister les consommateurs, révoquer :

```
aws ssm put-parameter --name /docker-repo/tokens/alice --type SecureString --value "NOUVEAU_TOKEN" --overwrite
```
```
aws ssm get-parameters-by-path --path /docker-repo/tokens --query "Parameters[].Name" --output text
```
```
aws ssm delete-parameter --name /docker-repo/tokens/alice
```

Une révocation ou un changement prend effet au plus tard après `TokenCacheSeconds` secondes.

## Tirer une image (consommateur)

`REGISTRE` est la sortie `RegistryHost` de la pile, par exemple `docker.example.com`.

```
docker login REGISTRE -u alice
```
```
docker pull REGISTRE/gh/mon-image:latest
```

Saisir le token à l'invite du mot de passe. Sous Windows (cmd), éviter `echo TOKEN | docker login ... --password-stdin` : `echo` ajoute une espace avant le `|` et le token est refusé.

Kubernetes :

```
kubectl create secret docker-registry registre-prive --docker-server=REGISTRE --docker-username=alice --docker-password=TOKEN
```

Le proxy est en lecture seule : tout push par ce chemin est refusé.

## Pousser depuis GitHub Actions

Copier `examples/github-workflow.yml` dans `.github/workflows/` du dépôt qui construit l'image, puis créer son environnement GitHub `prod` avec les variables `AWS_REGION` et `ECR_PUSH_ROLE_ARN` (commandes dans « Déclarer un projet et un client »).

Le dépôt ECR `gh/<nom>` est créé automatiquement au premier push, avec les règles de rétention de la pile. Le nom doit être en minuscules. Les règles de rétention ne s'appliquent qu'aux dépôts créés après le déploiement : modifier les paramètres de la pile ne change pas les dépôts existants.

Depuis le 15 juillet 2026, les nouveaux dépôts GitHub (et ceux renommés ou transférés) émettent un jeton OIDC dont le sujet est `repo:<compte>@<id>/<depot>@<id>:...`. Le rôle accepte les deux formats ; renseigner `GitHubOwnerId` verrouille le nouveau format sur l'identifiant immuable du compte.

## Gérer les images (administrateur, avec identifiants AWS)

```
aws ecr describe-repositories --query "repositories[].repositoryName" --output text
```
```
aws ecr list-images --repository-name gh/mon-image --query "imageIds[].imageTag" --output text
```
```
aws ecr batch-delete-image --repository-name gh/mon-image --image-ids imageTag=v1
```
```
aws ecr delete-repository --repository-name gh/mon-image --force
```

## Ce qui est vérifié, ce qui ne l'est pas

Vérifié localement :

- `cfn-lint template.yaml template-role.yaml template-account.yaml` (cfn-lint 1.60.1) : aucune erreur ; les propriétés récentes (`CREATE_ON_PUSH`, `InvokedViaFunctionUrl`) et les noms d'actions IAM sont reconnus. `sam validate --lint` déclare les trois templates valides.
- `python3 tests/local_e2e.py` : le code de la Lambda et de la fonction CloudFront, extrait du template, est exécuté derrière des simulateurs de CloudFront, d'URL Lambda, d'ECR et de S3. Un vrai client Docker 29.9 (magasin d'images containerd) y réussit `docker login`, `docker pull` par tag et par digest, et se voit refuser les mauvais tokens et les dépôts hors périmètre. Le simulateur reproduit le comportement constaté en production sur les requêtes `HEAD` (voir ci-dessous).

Vérifié sur AWS, avec la pile de test d'une pull request déployée par le workflow :

- connexion par OIDC depuis l'environnement `prod`, avec le format de sujet à identifiants immuables ;
- import des exports de `template-account.yaml`, certificat et alias DNS de la distribution ;
- création automatique d'un dépôt ECR au premier push ;
- défi d'authentification (`401` avec `www-authenticate: Basic`), token accepté, mauvais token refusé ;
- `docker login` puis `docker pull` par tag et par digest d'une image multi-manifestes, par le nom de domaine du registre, avec téléchargement des couches directement depuis S3 ;
- aucun numéro de compte ni nom de domaine en clair dans les journaux publics du workflow.

Constaté sur AWS : la chaîne CloudFront + URL de fonction Lambda annonce `Content-Length: 0` sur toute réponse à une requête `HEAD`. containerd prenait cette valeur pour la taille du manifeste et refusait le pull par digest. Le proxy répond désormais `405` aux `HEAD` sur les manifestes, ce qui fait refaire la requête en `GET` à containerd. Correction validée sur AWS et par le test local, qui reproduit l'échec avec l'ancienne version.

Non vérifié :

- le push depuis GitHub Actions avec le rôle de push et `examples/github-workflow.yml` ;
- le workflow `cleanup-pr.yml`, qui ne s'exécute qu'à la fermeture d'une pull request.

Contrôles à faire après un déploiement :

```
curl -i https://REGISTRE/v2/
```

Attendu : `401` avec un en-tête `www-authenticate: Basic ...`.

```
curl -s -u alice:TOKEN https://REGISTRE/v2/
```

Attendu : `{}`.

```
docker pull REGISTRE/gh/mon-image@sha256:DIGEST
```

Attendu : le téléchargement des couches. Supprimer d'abord toute copie locale de l'image, sinon Docker ne télécharge rien.

## Limites

- Le token circule en authentification Basic à chaque requête, chiffré par TLS. Il est stocké chiffré dans SSM ; toute personne ayant accès en lecture à ces paramètres dans le compte AWS peut le lire.
- Le consommateur doit pouvoir joindre CloudFront et les domaines S3 d'AWS de la région.
- Un manifeste de plus de 4 Mo est refusé par le proxy (les couches ne sont pas concernées).
- Les requêtes `HEAD` sur un manifeste reçoivent `405 Method Not Allowed` (voir ci-dessus). Docker et containerd, donc aussi Kubernetes avec containerd, refont alors la requête en `GET` ; vérifié avec Docker 29.9. Un outil qui interroge les manifestes uniquement en `HEAD`, par exemple pour comparer des digests sans télécharger, recevra une erreur.

## Coûts

Les ordres de grandeur sont à confirmer sur les pages de tarification AWS : le poste principal est le stockage ECR (de l'ordre de 0,10 USD par Go et par mois), puis le transfert sortant vers Internet lors des pulls. CloudFront, Lambda et SSM restent marginaux pour un usage de ce type.

## Suppression

```
aws cloudformation delete-stack --stack-name docker-repo
```

Les dépôts ECR créés au premier push et les paramètres SSM des tokens ne font pas partie de la pile : ils restent en place (et le stockage reste facturé) tant qu'ils ne sont pas supprimés avec les commandes ci-dessus.
