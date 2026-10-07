/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/** Pictures sent with a task; the server re-checks every limit against the real bytes. */
export const MAX_IMAGES = 4;
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export const IMAGE_ACCEPT = '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp';

const MEDIA_BY_EXTENSION: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp'
};

export interface RunImageUpload {
  name: string;
  media_type: string;
  data: string;
}

export interface ScreenedImage {
  file: File;
  mediaType: string;
}

export interface ScreenResult {
  accepted: ScreenedImage[];
  errors: string[];
}

/** The type the file name claims, kept only when the browser's own type does not contradict it. */
function mediaTypeOf(file: File): string | null {
  const extension = file.name.split('.').pop()?.toLowerCase() ?? '';
  const fromName = MEDIA_BY_EXTENSION[extension];
  if (!fromName || (file.type && file.type !== fromName)) {
    return null;
  }
  return fromName;
}

/** Splits picked files into usable pictures and one plain-language error per refusal. */
export function screenImages(files: File[], alreadyAttached: number): ScreenResult {
  const accepted: ScreenedImage[] = [];
  const errors: string[] = [];
  for (const file of files) {
    const mediaType = mediaTypeOf(file);
    if (!mediaType) {
      errors.push(`${file.name} is not a PNG, JPG, JPEG or WEBP image.`);
    } else if (file.size > MAX_IMAGE_BYTES) {
      errors.push(`${file.name} is larger than ${MAX_IMAGE_BYTES / (1024 * 1024)} MB.`);
    } else if (alreadyAttached + accepted.length >= MAX_IMAGES) {
      const message = `Only ${MAX_IMAGES} images can be attached to one message.`;
      if (!errors.includes(message)) errors.push(message);
    } else {
      accepted.push({ file, mediaType });
    }
  }
  return { accepted, errors };
}

export function toUpload(file: File, mediaType: string): Promise<RunImageUpload> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`${file.name} could not be read.`));
    reader.onload = () => {
      const url = String(reader.result);
      resolve({ name: file.name, media_type: mediaType, data: url.slice(url.indexOf(',') + 1) });
    };
    reader.readAsDataURL(file);
  });
}
