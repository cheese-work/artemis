package main

import (
	"encoding/json"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type configuration struct {
	Server        string                     `json:"server"`
	ADB           string                     `json:"adb"`
	Proxy         string                     `json:"proxy,omitempty"`
	DNSServer     string                     `json:"dns_server,omitempty"`
	NoADBDownload bool                       `json:"no_adb_download"`
	Extra         map[string]json.RawMessage `json:"-"`
}

func defaultPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".smartqa", "config.json")
}

func loadConfig(filename string, flags map[string]string) (configuration, error) {
	config := configuration{ADB: "adb", Extra: map[string]json.RawMessage{}}
	data, err := os.ReadFile(filename)
	if err == nil {
		if err = json.Unmarshal(data, &config); err != nil {
			return config, failure("SQH-E002", err)
		}
		if err = json.Unmarshal(data, &config.Extra); err != nil {
			return config, failure("SQH-E002", err)
		}
	} else if !os.IsNotExist(err) {
		return config, failure("SQH-E002", err)
	}
	for _, key := range []string{"server", "adb", "proxy", "dns_server", "no_adb_download"} {
		value, exists := os.LookupEnv("SMARTQA_HOST_" + strings.ToUpper(key))
		if flag, set := flags[key]; set {
			value, exists = flag, true
		}
		if !exists {
			continue
		}
		switch key {
		case "server":
			config.Server = value
		case "adb":
			config.ADB = value
		case "proxy":
			config.Proxy = value
		case "dns_server":
			config.DNSServer = value
		case "no_adb_download":
			config.NoADBDownload, err = strconv.ParseBool(value)
			if err != nil {
				return config, failure("SQH-E002", err)
			}
		}
	}
	if config.Server != "" {
		if _, err := serverURL(config.Server); err != nil {
			return config, err
		}
	}
	if config.Proxy != "" {
		proxy, err := url.Parse(config.Proxy)
		if err != nil || proxy.Host == "" || (proxy.Scheme != "http" && proxy.Scheme != "https") {
			return config, failure("SQH-E002", nil)
		}
	}
	if config.DNSServer != "" {
		address, _, err := net.SplitHostPort(config.DNSServer)
		if err != nil || net.ParseIP(address) == nil {
			return config, failure("SQH-E002", nil)
		}
	}
	return config, nil
}

func serverURL(value string) (*url.URL, error) {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || (parsed.Path != "" && parsed.Path != "/") {
		return nil, failure("SQH-E002", nil)
	}
	loopback := parsed.Hostname() == "localhost"
	if address := net.ParseIP(parsed.Hostname()); address != nil {
		loopback = address.IsLoopback()
	}
	if parsed.Scheme != "https" && !(parsed.Scheme == "http" && loopback) {
		return nil, failure("SQH-E002", nil)
	}
	return parsed, nil
}

func saveConfig(filename string, config configuration) error {
	encoded, err := json.Marshal(config)
	if err != nil {
		return failure("SQH-E002", err)
	}
	values := config.Extra
	if values == nil {
		values = map[string]json.RawMessage{}
	}
	for _, key := range []string{"server", "adb", "proxy", "dns_server", "no_adb_download"} {
		delete(values, key)
	}
	var known map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &known); err != nil {
		return err
	}
	for key, value := range known {
		values[key] = value
	}
	return saveJSON(filename, values)
}

func saveJSON(filename string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return failure("SQH-E002", err)
	}
	directory := filepath.Dir(filename)
	if err = os.MkdirAll(directory, 0700); err != nil {
		return failure("SQH-E002", err)
	}
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0077 != 0 {
		return failure("SQH-E002", fmt.Errorf("state directory must be private: %s", directory))
	}
	temporary, err := os.CreateTemp(directory, ".smartqa-*")
	if err != nil {
		return failure("SQH-E002", err)
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err = temporary.Chmod(0600); err == nil {
		_, err = temporary.Write(append(data, '\n'))
	}
	if err == nil {
		err = temporary.Sync()
	}
	closeErr := temporary.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(name, filename)
	}
	if err != nil {
		return failure("SQH-E002", err)
	}
	return nil
}
