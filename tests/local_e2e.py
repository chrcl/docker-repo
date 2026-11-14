#!/usr/bin/env python3
"""Test local de bout en bout du proxy de pull, sans compte AWS.

Le code teste est extrait de template.yaml (Lambda et fonction CloudFront), puis
execute derriere trois faux services HTTP sur 127.0.0.1 :

  client -> faux CloudFront -> fausse URL de fonction Lambda -> faux ECR -> faux S3

Les faux services reproduisent les comportements defavorables d'AWS :
  - la fausse URL Lambda renomme WWW-Authenticate ; sur les reponses HEAD elle
    annonce soit la longueur du corps renvoye par la fonction, soit 0. Le second
    cas est celui constate en production (CloudFront + URL de fonction) ; les
    deux sont testes ;
  - le faux CloudFront ne transmet que les en-tetes de la politique d'origine et
    refuse toute methode autre que GET/HEAD ;
  - le faux ECR exige le jeton ECR et redirige les couches vers le faux S3 ;
  - le faux S3 refuse toute requete portant un en-tete Authorization.

Usage : python3 tests/local_e2e.py
Prerequis : PyYAML, node. Si un demon Docker repond, un vrai docker login /
docker pull est execute en plus des verifications HTTP.
"""
import base64
import datetime
import gzip
import hashlib
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INLINE_CODE_LIMIT = 4000
ECR_PASSWORD = "fake-ecr-password"
TOKENS = {
    "alice": "alice-token-0123456789",
    "bob": "bob-token-0123456789 gh/demo,gh/team-*",
}
ORIGIN_HEADERS = ("accept", "x-registry-auth")
# Autre hote que le registre, comme S3 : un client HTTP ne renvoie pas ses
# identifiants lors d'une redirection vers un autre hote.
S3_ADDRESS = "127.0.0.2"
FILE_NAME, FILE_BODY = "hello.txt", b"bonjour depuis le faux ECR\n"


# ------------------------------------------------------------ template.yaml
class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, _suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


CfnLoader.add_multi_constructor("!", _tag)

with open(os.path.join(ROOT, "template.yaml"), encoding="utf-8") as fh:
    RESOURCES = yaml.load(fh, Loader=CfnLoader)["Resources"]
LAMBDA_CODE = RESOURCES["ProxyFunction"]["Properties"]["Code"]["ZipFile"]
EDGE_CODE = RESOURCES["ChallengeFunction"]["Properties"]["FunctionCode"]


# ------------------------------------------------------------ image de test
def build_image():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(FILE_NAME)
        info.size = len(FILE_BODY)
        tar.addfile(info, io.BytesIO(FILE_BODY))
    layer = gzip.compress(raw.getvalue(), mtime=0)
    config = json.dumps({
        "architecture": "amd64", "os": "linux", "config": {},
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(raw.getvalue()).hexdigest()]},
    }).encode()

    def descriptor(media_type, data):
        return {"mediaType": media_type, "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)}

    manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": descriptor("application/vnd.oci.image.config.v1+json", config),
        "layers": [descriptor("application/vnd.oci.image.layer.v1.tar+gzip", layer)],
    }, indent=3).encode()
    blobs = {"sha256:" + hashlib.sha256(b).hexdigest(): b for b in (layer, config)}
    return manifest, "sha256:" + hashlib.sha256(manifest).hexdigest(), blobs


MANIFEST, MANIFEST_DIGEST, BLOBS = build_image()
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
REPOSITORIES = ("gh/demo", "gh/private")
STATE = {}


# ------------------------------------------------------------ faux services
class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def send(self, status, headers=None, body=b"", length=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class FakeS3(Base):
    def do_GET(self):
        digest = self.path.split("?")[0].rsplit("/", 1)[-1]
        if self.headers.get("Authorization") or "X-Amz-Signature=fake" not in self.path or digest not in BLOBS:
            return self.send(400, body=b"bad request")
        self.send(200, {"Content-Type": "application/octet-stream"}, BLOBS[digest])


class FakeEcr(Base):
    def do_GET(self):
        expected = "Basic " + base64.b64encode(("AWS:" + ECR_PASSWORD).encode()).decode()
        if self.headers.get("Authorization") != expected:
            return self.send(401, {"WWW-Authenticate": 'Basic realm="ecr"'})
        path = self.path.split("?")[0]
        for repo in REPOSITORIES:
            prefix = "/v2/" + repo + "/"
            if not path.startswith(prefix):
                continue
            kind, _, ref = path[len(prefix):].partition("/")
            if kind == "manifests" and ref in ("latest", MANIFEST_DIGEST):
                if MANIFEST_TYPE not in self.headers.get("Accept", ""):
                    return self.send(404, body=b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
                headers = {"Content-Type": MANIFEST_TYPE, "Docker-Content-Digest": MANIFEST_DIGEST}
                return self.send(200, headers, MANIFEST)
            if kind == "blobs" and ref in BLOBS:
                url = "http://%s:%d/layers/%s?X-Amz-Signature=fake" % (S3_ADDRESS, STATE["s3"], ref)
                return self.send(307, {"Location": url})
            if kind == "tags" and ref == "list":
                body = json.dumps({"name": repo, "tags": ["latest"]}).encode()
                return self.send(200, {"Content-Type": "application/json"}, body)
        self.send(404, {"Content-Type": "application/json"}, b'{"errors":[{"code":"NAME_UNKNOWN"}]}')

    do_HEAD = do_GET


class FakeFunctionUrl(Base):
    """Convertit la requete en evenement Lambda (format 2.0) et appelle handler()."""

    def do_GET(self):
        path, _, query = self.path.partition("?")
        event = {
            "rawPath": path,
            "rawQueryString": query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "requestContext": {"http": {"method": self.command}},
        }
        result = STATE["handler"](event, None)
        body = base64.b64decode(result["body"]) if result.get("isBase64Encoded") else result["body"].encode()
        headers = {}
        for key, value in result["headers"].items():
            headers["x-amzn-remapped-" + key if key == "www-authenticate" else key] = value
        zero = self.command == "HEAD" and STATE["head_length"] == "zero"
        self.send(result["statusCode"], headers, body, length=0 if zero else None)

    do_HEAD = do_POST = do_PUT = do_GET


class FakeCloudFront(Base):
    def do_GET(self):
        path, _, query = self.path.partition("?")
        event = {"request": {
            "method": self.command, "uri": path, "querystring": {}, "cookies": {},
            "headers": {k.lower(): {"value": v} for k, v in self.headers.items()},
        }}
        result = run_edge_function(event)
        if "statusCode" in result:
            headers = {k: v["value"] for k, v in result.get("headers", {}).items()}
            return self.send(result["statusCode"], headers)
        forwarded = {k: v["value"] for k, v in result["headers"].items() if k in ORIGIN_HEADERS}
        conn = http.client.HTTPConnection("127.0.0.1", STATE["url"], timeout=30)
        conn.request(self.command, path + ("?" + query if query else ""), headers=forwarded)
        res = conn.getresponse()
        body = res.read()
        headers = {k: v for k, v in res.getheaders() if k.lower() not in ("content-length", "date", "server")}
        self.send(res.status, headers, body, length=int(res.getheader("Content-Length", "0")))
        conn.close()

    do_HEAD = do_GET

    def do_POST(self):
        self.send(403, body=b"This distribution is not configured to allow the HTTP request method")

    do_PUT = do_PATCH = do_DELETE = do_POST


def run_edge_function(event):
    script = EDGE_CODE + "\nprocess.stdout.write(JSON.stringify(handler(JSON.parse(process.argv[1]))));"
    out = subprocess.run(["node", "-e", script, json.dumps(event)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def serve(handler_class, address="127.0.0.1"):
    server = ThreadingHTTPServer((address, 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


# ------------------------------------------------------------ fausse AWS
def load_lambda():
    class ParameterNotFound(Exception):
        pass

    class Ssm:
        exceptions = types.SimpleNamespace(ParameterNotFound=ParameterNotFound)
        calls = 0

        def get_parameter(self, Name, WithDecryption):
            Ssm.calls += 1
            user = Name.rsplit("/", 1)[-1]
            if not Name.startswith("/test-stack/tokens/") or not WithDecryption or user not in TOKENS:
                raise ParameterNotFound(Name)
            return {"Parameter": {"Value": TOKENS[user]}}

    class Ecr:
        def get_authorization_token(self):
            expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=12)
            token = base64.b64encode(("AWS:" + ECR_PASSWORD).encode()).decode()
            return {"authorizationData": [{"authorizationToken": token, "expiresAt": expires}]}

    fake = types.ModuleType("boto3")
    fake.client = lambda name: {"ssm": Ssm(), "ecr": Ecr()}[name]
    sys.modules["boto3"] = fake
    http.client.HTTPSConnection = http.client.HTTPConnection  # le faux ECR parle HTTP
    os.environ.update(ECR_HOST="127.0.0.1:%d" % STATE["ecr"], TOKEN_PATH="/test-stack/tokens/", CACHE_TTL="60")
    namespace = {}
    exec(compile(LAMBDA_CODE, "index.py", "exec"), namespace)
    return namespace["handler"], Ssm


# ------------------------------------------------------------ verifications
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def call(method, path, user=None, token=None, accept=None):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (STATE["cf"], path), method=method)
    if user:
        request.add_header("Authorization", "Basic " + base64.b64encode((user + ":" + token).encode()).decode())
    if accept:
        request.add_header("Accept", accept)
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=30)
    except urllib.error.HTTPError as err:
        response = err
    return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()


RESULTS = []


def check(label, condition, detail=""):
    RESULTS.append(bool(condition))
    print("  [%s] %s%s" % ("OK" if condition else "ECHEC", label, " : " + str(detail) if detail and not condition else ""))


def http_checks(ssm):
    alice = TOKENS["alice"]
    bob = TOKENS["bob"].split()[0]
    manifest_path = "/v2/gh/demo/manifests/latest"

    status, headers, _ = call("GET", "/v2/")
    check("sans identifiants : 401 avec defi WWW-Authenticate Basic",
          status == 401 and headers.get("www-authenticate", "").startswith("Basic "), (status, headers))
    status, _, body = call("GET", "/v2/", "alice", alice)
    check("token valide : 200 sur /v2/", status == 200 and body == b"{}", (status, body))
    for user, token, label in (("alice", "mauvais", "mauvais token"), ("inconnu", alice, "utilisateur inconnu"),
                               ("../alice", alice, "nom d'utilisateur invalide"), ("alice", "", "token vide")):
        status, _, _ = call("GET", "/v2/", user, token)
        check(label + " : 401", status == 401, status)

    status, headers, body = call("GET", manifest_path, "alice", alice, MANIFEST_TYPE)
    check("manifeste : octets identiques a ceux du registre", status == 200 and body == MANIFEST, status)
    check("manifeste : Content-Type et Docker-Content-Digest transmis",
          headers.get("content-type") == MANIFEST_TYPE and headers.get("docker-content-digest") == MANIFEST_DIGEST, headers)
    status, _, _ = call("GET", manifest_path, "alice", alice, "application/json")
    check("en-tete Accept transmis au registre (404 si type non accepte)", status == 404, status)
    status, headers, _ = call("HEAD", manifest_path, "alice", alice, MANIFEST_TYPE)
    check("HEAD manifeste : 405 avec Allow: GET (le client refait un GET)", status == 405 and headers.get("allow") == "GET", (status, headers))
    status, _, _ = call("HEAD", manifest_path)
    check("HEAD manifeste sans identifiants : 401 avant le 405", status == 401, status)

    blob = next(iter(BLOBS))
    status, headers, _ = call("GET", "/v2/gh/demo/blobs/" + blob, "alice", alice)
    location = headers.get("location", "")
    check("couche : redirection 307 vers l'URL pre-signee, sans suivi par le proxy",
          status == 307 and location.startswith("http://%s:%d/" % (S3_ADDRESS, STATE["s3"])), (status, location))
    with urllib.request.urlopen(location, timeout=30) as response:
        check("couche : telechargement direct identique", response.read() == BLOBS[blob])
    status, _, body = call("GET", "/v2/gh/demo/tags/list", "alice", alice)
    check("liste des tags", status == 200 and json.loads(body)["tags"] == ["latest"], (status, body))

    status, _, _ = call("GET", manifest_path, "bob", bob, MANIFEST_TYPE)
    check("token limite : depot autorise -> 200", status == 200, status)
    status, _, _ = call("GET", "/v2/gh/private/manifests/latest", "bob", bob, MANIFEST_TYPE)
    check("token limite : depot non autorise -> 403", status == 403, status)
    status, _, _ = call("GET", "/v2/gh/private/manifests/latest", "alice", alice, MANIFEST_TYPE)
    check("token non limite : acces a tous les depots", status == 200, status)

    status, _, _ = call("GET", "/v2/_catalog", "alice", alice)
    check("chemin hors API de pull : 404", status == 404, status)
    status, _, _ = call("POST", "/v2/gh/demo/blobs/uploads/", "alice", alice)
    check("push via le proxy refuse", status == 403, status)
    check("tokens mis en cache (un seul appel SSM par utilisateur)", ssm.calls <= 4, ssm.calls)


def docker_checks():
    """Appelee une fois par hypothese sur le Content-Length des reponses HEAD."""
    registry = "127.0.0.1:%d" % STATE["cf"]
    env = dict(os.environ, DOCKER_CONFIG=tempfile.mkdtemp(prefix="docker-config-"))

    def docker(*args, stdin=None):
        return subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, env=env)

    def login(user, token):
        return docker("login", registry, "-u", user, "--password-stdin", stdin=token)

    image = registry + "/gh/demo:latest"
    private = registry + "/gh/private:latest"
    for ref in (image, private):
        docker("image", "rm", "-f", ref)

    result = docker("pull", image)
    check("docker pull sans login : refuse", result.returncode != 0, result.stdout)
    result = login("alice", "mauvais-token")
    check("docker login avec un mauvais token : refuse", result.returncode != 0, result.stdout)
    result = login("alice", TOKENS["alice"])
    check("docker login avec un token valide", result.returncode == 0, result.stderr)
    result = docker("pull", image)
    check("docker pull", result.returncode == 0, result.stderr)
    result = docker("image", "inspect", image, "--format", "{{json .RepoDigests}}")
    check("digest de l'image tiree = digest du registre", MANIFEST_DIGEST in result.stdout, result.stdout + result.stderr)
    container = docker("create", image, "/none").stdout.strip()
    target = tempfile.mkdtemp(prefix="pulled-")
    docker("cp", container + ":/" + FILE_NAME, target)
    docker("rm", container)
    pulled = os.path.join(target, FILE_NAME)
    check("contenu de la couche intact", os.path.exists(pulled) and open(pulled, "rb").read() == FILE_BODY)
    # Sans copie locale, le pull par digest passe vraiment par la resolution HEAD.
    docker("image", "rm", "-f", image)
    result = docker("pull", registry + "/gh/demo@" + MANIFEST_DIGEST)
    check("docker pull par digest", result.returncode == 0, result.stderr)

    login("bob", TOKENS["bob"].split()[0])
    result = docker("pull", private)
    check("docker pull d'un depot hors perimetre du token : refuse", result.returncode != 0, result.stdout)
    for ref in (image, private):
        docker("image", "rm", "-f", ref)


def main():
    check("code Lambda en ligne sous %d octets" % INLINE_CODE_LIMIT,
          len(LAMBDA_CODE.encode()) < INLINE_CODE_LIMIT, len(LAMBDA_CODE.encode()))
    STATE["s3"] = serve(FakeS3, S3_ADDRESS)
    STATE["ecr"] = serve(FakeEcr)
    STATE["handler"], ssm = load_lambda()
    STATE["url"] = serve(FakeFunctionUrl)
    STATE["cf"] = serve(FakeCloudFront)
    STATE["head_length"] = "body"
    print("Verifications HTTP")
    http_checks(ssm)
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("Pas de demon Docker : tests docker login / docker pull ignores")
    else:
        for mode, label in (("body", "longueur du corps"), ("zero", "0")):
            STATE["head_length"] = mode
            print("Client Docker, Content-Length des reponses HEAD = " + label)
            docker_checks()
    print("%d/%d verifications reussies" % (sum(RESULTS), len(RESULTS)))
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
