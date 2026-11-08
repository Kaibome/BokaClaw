.PHONY: build server-build server-test agent-loop-test agent-loop-lint dashboard-build docs lint

build: server-build dashboard-build

server-build:
	docker build -f server/Dockerfile -t bokaclaw-server .

server-test:
	cd server && python -m pytest tests ../tests

agent-loop-test:
	cd agent-loop && hatch run test

agent-loop-lint:
	cd agent-loop && hatch run lint

dashboard-build:
	cd dashboard && pnpm build

docs:
	cd docs && mintlify dev

lint:
	ruff check server/app server/tests agent-loop/src agent-loop/tests tests
