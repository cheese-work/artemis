package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"strings"
)

const toolsVersion = "37.0.1"
const maxArtifactBytes = 128 * 1024 * 1024

var toolsPins = map[string]struct{ Platform, Hash string }{
	"linux":   {"linux", "d230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1"},
	"darwin":  {"darwin", "ee39ad5967e95c2a07f04dbcbde96b1a0c916ba376096db5d2f498b7727a5d1d"},
	"windows": {"win", "45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99"},
}

func verifySHA256(data []byte, expected string) error {
	digest := sha256.Sum256(data)
	if hex.EncodeToString(digest[:]) != expected {
		return failure("SQH-E202", nil)
	}
	return nil
}

func download(ctx context.Context, config configuration, address string) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, "GET", address, nil)
	if err != nil {
		return nil, failure("SQH-E104", err)
	}
	response, err := httpClient(config).Do(request)
	if err != nil {
		return nil, failure(networkCode(err), err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, failure("SQH-E104", nil)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxArtifactBytes+1))
	if err != nil || len(data) > maxArtifactBytes {
		return nil, failure("SQH-E203", err)
	}
	return data, nil
}

func extractTools(data []byte, directory string) error {
	reader, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return failure("SQH-E203", err)
	}
	var total uint64
	for _, entry := range reader.File {
		clean := path.Clean(entry.Name)
		if strings.Contains(entry.Name, "\\") || strings.HasPrefix(entry.Name, "/") || clean != strings.TrimSuffix(entry.Name, "/") || !strings.HasPrefix(clean, "platform-tools/") || entry.Mode()&os.ModeSymlink != 0 || (!entry.FileInfo().IsDir() && !entry.Mode().IsRegular()) {
			if entry.FileInfo().IsDir() && entry.Name == "platform-tools/" {
				continue
			}
			return failure("SQH-E203", nil)
		}
		if entry.UncompressedSize64 > maxArtifactBytes || total > maxArtifactBytes-entry.UncompressedSize64 {
			return failure("SQH-E203", nil)
		}
		total += entry.UncompressedSize64
	}
	for _, entry := range reader.File {
		destination := filepath.Join(directory, filepath.FromSlash(entry.Name))
		if entry.FileInfo().IsDir() {
			if err = os.MkdirAll(destination, 0700); err != nil {
				return err
			}
			continue
		}
		if err = os.MkdirAll(filepath.Dir(destination), 0700); err != nil {
			return err
		}
		mode := os.FileMode(0600)
		if entry.Mode().Perm()&0111 != 0 || path.Base(entry.Name) == "adb" {
			mode = 0700
		}
		file, err := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
		if err != nil {
			return failure("SQH-E203", err)
		}
		source, err := entry.Open()
		if err != nil {
			_ = file.Close()
			return failure("SQH-E203", err)
		}
		_, copyErr := io.Copy(file, io.LimitReader(source, int64(entry.UncompressedSize64)+1))
		sourceErr := source.Close()
		closeErr := file.Close()
		if copyErr != nil {
			return failure("SQH-E203", copyErr)
		}
		if sourceErr != nil {
			return failure("SQH-E203", sourceErr)
		}
		if closeErr != nil {
			return failure("SQH-E203", closeErr)
		}
	}
	return nil
}

func ensureADB(ctx context.Context, configPath string, config configuration) (string, error) {
	if binary, err := exec.LookPath(config.ADB); err == nil {
		return binary, nil
	}
	if config.NoADBDownload || config.ADB != "adb" {
		return "", failure("SQH-E201", nil)
	}
	if !hostAgentEnabled() {
		return "", failure("SQH-E001", nil)
	}
	pin, exists := toolsPins[runtime.GOOS]
	if !exists {
		return "", failure("SQH-E402", nil)
	}
	directory := filepath.Join(filepath.Dir(configPath), "tools", toolsVersion)
	binaryName := "adb"
	if runtime.GOOS == "windows" {
		binaryName += ".exe"
	}
	binary := filepath.Join(directory, "platform-tools", binaryName)
	if info, err := os.Stat(binary); err == nil && info.Mode().IsRegular() {
		return binary, nil
	}
	parent := filepath.Dir(directory)
	if err := os.MkdirAll(parent, 0700); err != nil {
		return "", failure("SQH-E201", err)
	}
	address := "https://dl.google.com/android/repository/platform-tools_r" + toolsVersion + "-" + pin.Platform + ".zip"
	data, err := download(ctx, config, address)
	if err != nil {
		return "", err
	}
	if err = verifySHA256(data, pin.Hash); err != nil {
		return "", err
	}
	temporary, err := os.MkdirTemp(parent, ".download-*")
	if err != nil {
		return "", failure("SQH-E201", err)
	}
	defer os.RemoveAll(temporary)
	if err = extractTools(data, temporary); err != nil {
		return "", err
	}
	if info, err := os.Stat(filepath.Join(temporary, "platform-tools", binaryName)); err != nil || !info.Mode().IsRegular() {
		return "", failure("SQH-E203", err)
	}
	if err = os.Rename(temporary, directory); err != nil {
		return "", failure("SQH-E201", err)
	}
	return binary, nil
}
