#!/bin/bash
name="linuxdeepin"
while true;do
	let page++
	sleep 1
	wget "https://api.github.com/users/$name/repos?per_page=100&page=$page" -O repos_$page.json
	n=`cat repos_$page.json | jq '.[]|.full_name' | wc -l`
	echo $n
	if [ "x$n" != "x100" ]; then
      		break
  	fi
done
ls -l
