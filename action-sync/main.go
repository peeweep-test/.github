package main

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/bradleyfalzon/ghinstallation"
	"github.com/google/go-github/github"
)

func main() {

	privateKey := []byte(os.Getenv("PRIVATE_KEY"))
	changedList := os.Getenv("CHANGED_LIST")
	var appID, installationID int64
	var message string
	var dryRun bool
	flag.Int64Var(&appID, "app_id", 0, "*github app id")
	flag.Int64Var(&installationID, "installation_id", 0, "*github installation id")
	flag.StringVar(&message, "message", "chore: Sync by "+os.Getenv("GITHUB_REPOSITORY"), "*commit message")
	flag.BoolVar(&dryRun, "dryRun", false, "")
	flag.Parse()
	if appID == 0 || installationID == 0 {
		flag.PrintDefaults()
		return
	}

	itr, err := ghinstallation.New(http.DefaultTransport, appID, installationID, []byte(privateKey))
	if err != nil {
		panic(err)
	}
	client := github.NewClient(&http.Client{Transport: itr})
	ctx := context.Background()

	files := strings.Fields(changedList)
	// Sync all repositories if workflows changed
	if len(changedList) == 0 {
		files, err = findFile("repos")
		if err != nil {
			panic(err)
		}
	}
	for _, file := range files {
		if !strings.HasPrefix(file, "repos") {
			continue
		}
		data, err := os.ReadFile(file)
		if err != nil {
			log.Fatal(err)
		}
		var configs []struct {
			Src     string   `json:"src"`
			Dest    string   `json:"dest"`
			Branche []string `json:"branche"`
		}
		err = json.Unmarshal(data, &configs)
		if err != nil {
			log.Fatal(err)
		}
		log.Println("Config", file)
		for _, config := range configs {
			arr := strings.SplitN(config.Dest, "/", 3)
			if len(arr) != 3 {
				log.Fatal("wrong dist. example: username/repo/file")
			}
			owner := arr[0]
			repo := arr[1]
			path := arr[2]
			log.Printf("\tSync %s to %s/%s/%s", config.Src, owner, repo, path)
			if dryRun {
				continue
			}
			err = sendFile(ctx, client, config.Src, owner, repo, path, message, config.Branche)
			if err != nil {
				log.Fatal(err)
			}
		}
	}

}

func findFile(root string) ([]string, error) {
	var files []string
	return files, filepath.Walk("repos", func(path string, info fs.FileInfo, err error) error {
		if !info.IsDir() {
			files = append(files, path)
		}
		return nil
	})
}

func sendFile(ctx context.Context, client *github.Client, localFile string, owner, repo, path, message string, syncBranches []string) error {
	if syncBranches == nil {

		branches, _, err := client.Repositories.ListBranches(ctx, owner, repo, nil)
		if err != nil {
			return err
		}
		for i := range branches {
			syncBranches = append(syncBranches, *branches[i].Name)
		}
	}
	for _, branche := range syncBranches {
		fileContent, _, resp, err := client.Repositories.GetContents(
			ctx, owner, repo, path,
			&github.RepositoryContentGetOptions{Ref: branche},
		)
		if err != nil {
			if resp.StatusCode != http.StatusNotFound {
				panic(err)
			}
		}
		var latestSha string
		if fileContent != nil {
			latestSha = fileContent.GetSHA()
		}
		content, err := os.ReadFile(localFile)
		if err != nil {
			panic(err)
		}
		sha := sha1.New()
		sha.Write([]byte(fmt.Sprintf("blob %d", len(content))))
		sha.Write([]byte{0})
		sha.Write(content)
		currentSha := hex.EncodeToString(sha.Sum(nil))
		if string(latestSha) == currentSha {
			log.Println("\t\tBranche", branche, " no change")
			continue
		}
		log.Println("\t\tBranche", owner, repo, path, message)
		_, _, err = client.Repositories.UpdateFile(
			ctx, owner, repo, path,
			&github.RepositoryContentFileOptions{
				Message: &message,
				Content: content,
				SHA:     &latestSha,
				Branch:  &branche,
			},
		)
		if err != nil {
			return err
		}
	}
	return nil
}
