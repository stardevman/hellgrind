.PHONY: up down reset smoke test logs ps panel

up:        ## Tüm servisleri derle ve başlat
	docker compose up -d --build

smoke:     ## Uçtan uca duman testi
	docker compose exec api python -m scripts.smoke

test:      ## Testler (db/ klasörü bağlanır: servisler varsa DB/depo testleri ayrı hellgrind_test veritabanında koşar)
	docker compose run --rm -v "$(CURDIR)/db:/db:ro" api python -m pytest -q

panel:     ## Paneli tarayıcıda aç (macOS/Linux)
	@python3 -m webbrowser http://localhost:8000 || true

logs:
	docker compose logs -f api worker

ps:
	docker compose ps

down:
	docker compose down

reset:     ## Veritabanı ve MinIO dahil her şeyi sil
	docker compose down -v
