# Deployment

The live Compose definition, Dockerfile and Caddyfile remain in `app/` to preserve the tested build context and volume paths. From the repository root, run:

```text
docker compose -f app/compose.yaml --project-directory app up --build
```

Fill `app/.env` from `.env.example` with real secrets provided outside Git. `PRIVATE_STORAGE` is `/data/private` in Compose. The PostgreSQL `pgdata` and private-files volumes require operator backups. The `backup_local` management command covers local development only; production database and media backups need an environment-specific procedure.
