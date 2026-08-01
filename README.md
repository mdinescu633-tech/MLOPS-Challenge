# MLOPS-Challenge

GitOps source of truth for a local Kubernetes platform, managed by [FluxCD](https://fluxcd.io/). Exposes a CPU-burning demo app (`loadtester`) behind ingress-nginx, secured with Keycloak + oauth2-proxy, and autoscaled with an HPA.

## Overview

This repository defines everything running on the cluster — ingress-nginx, Keycloak, metrics-server, and the `loadtester` app itself. Changes pushed to `main` are automatically picked up and reconciled by FluxCD, making the repo the single source of truth for what runs on the cluster. Secrets (Keycloak client secret, cookie secret, realm import) are encrypted with [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) so they can live safely in a public repo.

## Architecture

```
                              Git repository
                                    |
                                    | reconciles (every 10m or on push)
                                    v
                         ┌────────────────────┐
                         │   FluxCD (flux-     │
                         │   system namespace) │
                         └──────────┬──────────┘
                                    |
                applies             | applies
        ┌───────────────────────────┴───────────────────────────┐
        v                                                        v
┌───────────────────┐                                  ┌──────────────────┐
│  infrastructure/   │  (reconciled first, apps depend  │   apps/loadtester │
│                     │   on this via dependsOn)         │                   │
│  - ingress-nginx    │                                   │  - namespace     │
│  - keycloak +       │                                   │  - deployment    │
│    oauth2-proxy     │                                   │  - service       │
│  - metrics-server   │                                   │  - ingress       │
│  - sealed-secrets   │                                   │  - hpa           │
└───────────────────┘                                    └──────────────────┘
        |                                                        |
        | CPU/mem metrics                                        |
        v                                                        v
┌───────────────────┐   scales pods    ┌──────────────────────────────┐
│  metrics-server     │ ───────────────>│  loadtester HPA (1-5 pods,    │
└───────────────────┘                   │  target: 50% CPU)             │
                                          └──────────────────────────────┘

Request path:
  client --(1) get token--> Keycloak
  client --(2) request + bearer token--> ingress-nginx
                                              |
                                              | auth_request
                                              v
                                        oauth2-proxy --(3) validate token--> Keycloak
                                              |
                                              | authorized
                                              v
                                    loadtester Service --> loadtester pods
```

## Repository Structure

```bash
.
├── clusters/mlops/            # Flux entrypoint (infrastructure.yaml, apps.yaml)
├── infrastructure/            # ingress-nginx, keycloak, metrics-server, sealed-secrets
├── apps/loadtester/           # the app itself (deployment, service, ingress, hpa)
├── scripts/                   # load_test.py
├── .yamllint.yml
└── .github/workflows/         # CI: yamllint on push/PR
```

`apps.yaml` depends on `infrastructure.yaml` finishing first, since the app's ingress needs oauth2-proxy to already exist.

## How It Works

### Auth

Auth happens entirely at the ingress layer, via the `auth-url`/`auth-signin` annotations on the app's Ingress — the app itself has no auth code. nginx forwards every request to `oauth2-proxy` first, which validates the bearer token against Keycloak before letting the request through.

### Autoscaling

`metrics-server` reports CPU usage to the HPA, which scales `loadtester` pods between 1 and 5 replicas, targeting 50% average CPU utilization.

### Secret Management

Secrets are sealed with `kubeseal` before being committed — the client secret, cookie secret, and Keycloak realm import all live in the repo encrypted, and only the in-cluster Sealed Secrets controller can decrypt them. The plaintext `realm-export.json` is gitignored on purpose.

### Load Testing

`scripts/load_test.py` authenticates against Keycloak, fires concurrent requests at `/burn`, and polls `kubectl get pods`/`kubectl top pods` while it runs. Since `/burn` only allows one burn per pod at a time, it tracks accepted (202) separately from conflicts (409, pod already busy) and real errors (5xx/connection) instead of lumping them into one error rate.

## Initial Setup

Before bootstrapping, copy `infrastructure/keycloak/realm-export.example.json` to
`realm-export.json`, fill in your own client secret and user passwords, then seal it:
```bash
kubeseal --format yaml < realm-export.json > infrastructure/keycloak/keycloak-realm-import-sealed.yaml
```

1. Create the cluster (using k3d — swap this step for kind/minikube if needed):
   ```bash
   k3d cluster create mlops -p "80:80@loadbalancer" -p "443:443@loadbalancer"
   kubectl get nodes
   kubectl get pods -A
   ```
2. Bootstrap FluxCD:
   ```bash
   export GITHUB_TOKEN=<your-pat>
   flux bootstrap github --owner=<you> --repository=MLOPS-Challenge --branch=main --path=clusters/mlops --personal
   flux get source git
   flux get kustomizations
   ```
3. Check everything came up:
   ```bash
   kubectl get pods -n ingress-nginx -n keycloak -n loadtester
   kubectl top pods -A
   kubectl top nodes
   ```
4. Run the load test:
   ```bash
   cd scripts
   export CLIENT_SECRET=<client secret>
   export DEVUSER_PASSWORD=<devuser password>
   python load_test.py
   ```
   In another terminal, watch it scale:
   ```bash
   kubectl get pods -n loadtester -w
   ```

From this point on, any change pushed to `main` is picked up automatically by Flux.

## Technical Choices

**ingress-nginx** — went with it over Traefik/Envoy mainly because of the `auth-url`/`auth-signin` annotation pattern, which is the simplest way to delegate auth to oauth2-proxy without writing any custom logic.

**HPA over VPA/KEDA** — `/burn` is a plain CPU-bound workload, so CPU utilization is the most direct signal. VPA resizes existing pods rather than adding more, and KEDA's event-based scaling doesn't really apply here since there's no queue/event source — HPA is the simplest fit. Went with 50% average CPU utilization and 1-5 replicas after watching `kubectl top pods` under a real load test run.

**Keycloak + oauth2-proxy** — keeps OIDC entirely at the ingress layer instead of baking auth into the app.

**Sealed Secrets** — lets the client secret, cookie secret, and Keycloak realm import live in the repo encrypted, since this is a public repo.

**yamllint over yamlfmt** — the challenge only required one of the two, and yamllint catches actual structural/syntax errors, not just formatting.

## Known Limitations

- No TLS — everything runs over plain HTTP on `*.127.0.0.1.nip.io`, fine for local dev only.
- oauth2-proxy runs with a few `--insecure-*` flags, needed because Keycloak's internal and external URLs differ in this local setup.
- Single-node cluster — no multi-node scheduling gets exercised.
- HPA thresholds (50% CPU, max 5 replicas) came from watching `kubectl top pods` under load, not a formal capacity plan.
- `/burn` is single-flight per pod, so load test concurrency needs to stay close to the current pod count or most requests just come back as 409 instead of generating real load.
- Recreating the cluster from scratch isn't fully hands-off: Sealed Secrets generates a new key pair per cluster, so secrets sealed against the old cluster (`sealed-secret.yaml`, `keycloak-realm-import-sealed.yaml`) can't be decrypted by a fresh one. Either back up the sealed-secrets key before tearing the cluster down and restore it into the new one, or re-seal the secrets against the new cluster's key.

## License

[MIT](LICENSE)