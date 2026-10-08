package main

import "os"

type artifactManifest struct {
	SHA256 string
	Size   int64
}
type manifestVerifier interface {
	Verify([]byte, artifactManifest) error
}
type sha256Verifier struct{}

func (sha256Verifier) Verify(data []byte, manifest artifactManifest) error {
	if int64(len(data)) != manifest.Size {
		return failure("SQH-E202", nil)
	}
	return verifySHA256(data, manifest.SHA256)
}
func stageArtifact(filename string, data []byte, manifest artifactManifest, verifier manifestVerifier) error {
	if verifier == nil {
		return failure("SQH-E202", nil)
	}
	if err := verifier.Verify(data, manifest); err != nil {
		return err
	}
	file, err := os.OpenFile(filename, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return failure("SQH-E002", err)
	}
	_, writeErr := file.Write(data)
	closeErr := file.Close()
	if writeErr != nil {
		_ = os.Remove(filename)
		return failure("SQH-E002", writeErr)
	}
	if closeErr != nil {
		_ = os.Remove(filename)
		return failure("SQH-E002", closeErr)
	}
	return nil
}
