# All commands run inside the project: project-local Python/venv, and HOME is
# redirected to .home/ because Rasa, TensorFlow and Streamlit write config
# files to the home directory (e.g. ~/.config/rasa, ~/.keras, ~/.streamlit).
SHELL := /bin/bash
ROOT  := $(CURDIR)
VENV  := $(ROOT)/.venv
BIN   := $(VENV)/bin
ENV   := HOME=$(ROOT)/.home RASA_TELEMETRY_ENABLED=false \
         UV_PYTHON_INSTALL_DIR=$(ROOT)/.python UV_CACHE_DIR=$(ROOT)/.uv-cache \
         SQLALCHEMY_SILENCE_UBER_WARNING=1 TF_CPP_MIN_LOG_LEVEL=2

# Load API keys from .env if present (optional — every action has a fallback).
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: setup validate train actions rasa ui test test-core test-nlu shell clean

setup:            ## Create .venv (Python 3.10) and install dependencies
	mkdir -p .home
	$(ENV) uv python install 3.10
	$(ENV) uv venv --python 3.10 $(VENV)
	$(ENV) uv pip install --python $(BIN)/python -r requirements.txt

validate:         ## Check domain / stories / rules consistency
	$(ENV) $(BIN)/rasa data validate

train:            ## Train the Rasa model into models/
	$(ENV) $(BIN)/rasa train

actions:          ## Action server on :5055
	$(ENV) $(BIN)/rasa run actions --actions actions

rasa:             ## Rasa server (NLU + Core) on :5005, REST channel enabled
	$(ENV) $(BIN)/rasa run --enable-api --cors "*" --port 5005

ui:               ## Streamlit UI on :8501
	$(ENV) $(BIN)/streamlit run frontend/streamlit_app.py --server.headless true

shell:            ## Chat in the terminal (needs `make actions` running)
	$(ENV) $(BIN)/rasa shell

test:             ## pytest unit tests for custom actions
	$(ENV) $(BIN)/pytest tests/test_actions.py -v

test-core:        ## Dialogue tests (needs a trained model)
	$(ENV) $(BIN)/rasa test core --stories tests/test_stories.yml

test-nlu:         ## NLU cross-validation
	$(ENV) $(BIN)/rasa test nlu --cross-validation --folds 3

clean:
	rm -rf models results .rasa
