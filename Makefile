.PHONY: up down reset smoke test logs ps panel web-dev web-build web-test flow-bridge

up:        ## Tüm servisleri derle ve başlat
	docker compose up -d --build

smoke:     ## Uçtan uca duman testi
	docker compose exec api python -m scripts.smoke

test:      ## Tam test paketi (db/, panel kaynağı ve depo kökündeki iki dosya bağlanır; DB/depo testleri ayrı hellgrind_test veritabanında koşar)
	docker compose run --rm -v "$(CURDIR)/db:/db:ro" -v "$(CURDIR)/frontend/src:/frontend/src:ro" -v "$(CURDIR)/comfy-workflows:/comfy-workflows:ro" -v "$(CURDIR)/.gitignore:/.gitignore:ro" -v "$(CURDIR)/.env.example:/.env.example:ro" -e DRY_RUN=1 -e CONFIG_ENV_FILE= api python -m pytest -q

web-dev:   ## Panel geliştirme sunucusu (http://localhost:3000/panel/; /api çalışan API'ye, varsayılan :8000, yönlenir)
	cd frontend && npm install && npm run dev

web-build: ## Paneli statik olarak derle (frontend/out); imaj bunu kendisi yapar
	cd frontend && npm ci && npm run build

web-test:  ## Panelin tip denetimi ve birim testleri (Node 22)
	cd frontend && npm ci && npm run typecheck && npm test

flow-bridge: ## Google Flow köprüsü (Mac'te; gflow-cli ve bir kez "gflow auth login --browser chrome" gerekir). Belirteç .env'deki FLOW_BRIDGE_TOKEN
	FLOW_BRIDGE_TOKEN="$$(grep -E '^FLOW_BRIDGE_TOKEN=' .env | cut -d= -f2-)" python3 tools/flow_bridge/flow_bridge.py

panel:     ## Paneli tarayıcıda aç (macOS/Linux)
	@python3 -m webbrowser http://localhost:8000 || true

logs:
	docker compose logs -f api worker worker-gen

ps:
	docker compose ps

down:
	docker compose down

reset:     ## Veritabanı ve MinIO dahil her şeyi sil
	docker compose down -v
