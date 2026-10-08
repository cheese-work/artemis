package main

import (
	"errors"
	"fmt"
	"net"
	"strings"
)

var errorCatalog = map[string]string{
	"SQH-E001": "Host agent is disabled. Set ARTEMIS_HOST_AGENT=1 to opt in.",
	"SQH-E002": "Configuration or private state is invalid. Check config show and file permissions.",
	"SQH-E003": "Command is invalid. Run smartqa-host help.",
	"SQH-E004": "Enrollment failed. Create a new code in Setup → Computers.",
	"SQH-E005": "Enroll this agent before running it.",
	"SQH-E006": "The server requires a newer agent protocol. Update the agent.",
	"SQH-E007": "Host authentication is expired or revoked. Re-enroll this computer.",
	"SQH-E008": "The host tunnel protocol is invalid. Check the server and agent versions.",
	"SQH-E009": "The local agent is unavailable or already running. Check service status.",
	"SQH-E101": "DNS lookup failed. Check split-DNS VPN settings or SMARTQA_HOST_DNS_SERVER.",
	"SQH-E102": "Proxy connection failed. Check SMARTQA_HOST_PROXY and proxy authentication.",
	"SQH-E103": "TLS verification failed. Check the certificate chain and system clock; do not disable TLS verification.",
	"SQH-E104": "Server connection failed. Check doctor and retry.",
	"SQH-E201": "adb is missing or unusable. Run doctor or install platform-tools.",
	"SQH-E202": "Artifact checksum does not match. Do not install this download.",
	"SQH-E203": "Artifact archive is unsafe or exceeds the size limit.",
	"SQH-E204": "adb request is denied by the non-overridable discovery allowlist.",
	"SQH-E205": "Device is absent, ambiguous or not authorized. Reconnect and run devices.",
	"SQH-E301": "B2 tunnel and gateway are not published. This agent cannot expose a device yet.",
	"SQH-E302": "Update activation needs the B3a-3 launcher. The verified artifact is staged only.",
	"SQH-E401": "User service operation failed. Run service status and check OS user-session support.",
	"SQH-E402": "This OS lifecycle is not implemented in B3a-1. Windows installer and protected state belong to B3b.",
}

type agentError struct {
	Code  string
	Cause error
}

const permanentErrorExitStatus = 78

func processExitStatus(err error) int {
	if err == nil {
		return 0
	}
	switch errorCode(err) {
	case "SQH-E006", "SQH-E007", "SQH-E008", "SQH-E009":
		return permanentErrorExitStatus
	}
	return 1
}

func (err *agentError) Error() string        { return fmt.Sprintf("%s: %s", err.Code, errorCatalog[err.Code]) }
func (err *agentError) Unwrap() error        { return err.Cause }
func failure(code string, cause error) error { return &agentError{code, cause} }
func errorCode(err error) string {
	var typed *agentError
	if errors.As(err, &typed) {
		return typed.Code
	}
	return "SQH-E104"
}
func networkCode(err error) string {
	if strings.Contains(strings.ToLower(err.Error()), "proxyconnect") {
		return "SQH-E102"
	}
	var dns *net.DNSError
	if errors.As(err, &dns) {
		return "SQH-E101"
	}
	text := strings.ToLower(err.Error())
	if strings.Contains(text, "tls") || strings.Contains(text, "x509") {
		return "SQH-E103"
	}
	return "SQH-E104"
}
