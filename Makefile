# ── Просмотр ──────────────────────────────────────
view-stats:
	python -m src.viewer --stats

view-last:
	python -m src.viewer --last 30

view-search:
	python -m src.viewer --search "$(QUERY)"

view-matched:
	python -m src.viewer --matched --product $(PRODUCT)

view-interactive:
	python -m src.viewer -i

# ── Экспорт ───────────────────────────────────────
export-csv:
	python -m src.viewer --export csv

export-excel:
	python -m src.viewer --export excel

export-html:
	python -m src.viewer --export html

export-matched-excel:
	python -m src.viewer --matched --export excel

export-all:
	python -m src.viewer --export all