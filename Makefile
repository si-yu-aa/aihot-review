.PHONY: run pull test check

run:
	python3 server.py

pull:
	python3 server.py --pull --hours 24

test:
	python3 -m unittest discover -s tests -v

check: test
	python3 -m py_compile server.py
	@if command -v node >/dev/null 2>&1; then node --check app.js; fi
