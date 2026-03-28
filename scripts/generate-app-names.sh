#!/usr/bin/env bash
set -euo pipefail

COUNT="${1:-300}"
OUTPUT_FILE="app-names.txt"

ADJECTIVES=(
  alpha bravo charlie delta echo foxtrot golf hotel india juliet
  kilo lima mike november oscar papa quebec romeo sierra tango
  ultra victor whiskey xray yankee zulu amber bronze coral dusk
  ember frost glow haze iris jade knot lunar mist nova
  opal pearl quartz reef silk tide volt wren apex bolt
  cedar drift eagle flint grove haven ivory jetty kelp lava
  mesa nimbus onyx prism ridge spark trove umbra vapor wave
)

NOUNS=(
  ant bear cat dog elk fox gnu hawk ibis jay
  kite lynx mole newt owl puma quail ram seal tern
  urchin viper wasp xerus yak zebu ape bass crab deer
  eel frog goat heron iguana jelly koala lemur moth narwhal
  otter panda ray swan tiger vole worm axon bark chip
  dial echo fuse gear hose iron jack keel lens mast
)

> "$OUTPUT_FILE"

declare -A SEEN

generated=0
while [ "$generated" -lt "$COUNT" ]; do
  adj=${ADJECTIVES[$((RANDOM % ${#ADJECTIVES[@]}))]}
  noun=${NOUNS[$((RANDOM % ${#NOUNS[@]}))]}
  num=$(printf "%03d" $((RANDOM % 1000)))
  name="app-${adj}-${noun}-${num}"

  if [ -z "${SEEN[$name]+_}" ]; then
    SEEN[$name]=1
    echo "$name" >> "$OUTPUT_FILE"
    generated=$((generated + 1))
  fi
done

echo "Generated $COUNT app names in $OUTPUT_FILE"
