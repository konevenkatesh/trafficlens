#!/bin/zsh
cd /Volumes/MySSD/Traffic_Count
DONE="ch01_20250916153959 ch01_20260716160000 ch01_20260716232714 ch03_20260704152639 ch03_20260704202846"
for f in /Volumes/RK/Traffic/*.mp4; do
  b=$(basename $f .mp4)
  [[ " $DONE " == *" $b "* ]] && continue
  ch=${b%%_*}; stamp=${b#*_}; date=${stamp:0:8}; hh=${stamp:8:2}; hhmmss=${stamp:8:6}
  if [[ $ch == ch01 && $date == 202509* ]]; then site=srisailam
  elif [[ $ch == ch01 && ($date == 20260716 || $date == 20260717) ]]; then site=bhalki
  elif [[ $ch == ch03 ]]; then site=tdp
  elif [[ $ch == ch01 && $date == 20260704 ]]; then site=atp
  else site=$ch; fi
  if (( hh >= 19 || hh <= 5 )); then rate="1/40"; else rate="1/15"; fi
  echo "sampling $b -> ${site}_${hhmmss} (fps=$rate)"
  ffmpeg -v error -i "$f" -vf fps=$rate -q:v 2 "dataset/frames_raw/${site}_${hhmmss}_%04d.jpg" </dev/null
done
echo TOTAL: $(ls dataset/frames_raw | wc -l)
