#!/bin/sh
# Ship HEAD to the NAS and rebuild. .env, data/ and the vault on the NAS are untouched.
# NAS=user@host (and optional SSH_OPTS) come from the environment or a gitignored .deploy.env.
set -e
[ -f "$(dirname "$0")/.deploy.env" ] && . "$(dirname "$0")/.deploy.env"
: "${NAS:?set NAS=user@host (or put it in .deploy.env)}"
DIR=/volume1/docker/nba-tracker
git -c core.autocrlf=false archive HEAD | ssh $SSH_OPTS "$NAS" "mkdir -p $DIR/data '/volume1/<USER>/Obsidian/NBA Tracker' && cd $DIR && tar xmf - && docker compose up -d --build"
