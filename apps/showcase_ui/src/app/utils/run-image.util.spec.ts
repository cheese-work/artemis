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

import { MAX_IMAGES, MAX_IMAGE_BYTES, newDraftId, screenImages, toUpload } from './run-image.util';

function file(name: string, type: string, size = 10): File {
  return new File([new Uint8Array(size)], name, { type });
}

describe('screenImages', () => {
  it('accepts png, jpg, jpeg and webp', () => {
    const picked = screenImages(
      [file('a.png', 'image/png'), file('b.JPG', 'image/jpeg'), file('c.jpeg', 'image/jpeg'), file('d.webp', 'image/webp')],
      0
    );

    expect(picked.errors).toEqual([]);
    expect(picked.accepted.map((p) => p.mediaType)).toEqual(['image/png', 'image/jpeg', 'image/jpeg', 'image/webp']);
  });

  it('names each refused file and why, and keeps the rest', () => {
    const picked = screenImages([file('a.gif', 'image/gif'), file('doc.pdf', 'application/pdf'), file('ok.png', 'image/png')], 0);

    expect(picked.accepted.map((p) => p.file.name)).toEqual(['ok.png']);
    expect(picked.errors).toEqual([
      'a.gif is not a PNG, JPG, JPEG or WEBP image.',
      'doc.pdf is not a PNG, JPG, JPEG or WEBP image.'
    ]);
  });

  it('refuses a file whose name and type disagree', () => {
    const picked = screenImages([file('fake.png', 'image/gif')], 0);

    expect(picked.accepted).toEqual([]);
    expect(picked.errors.length).toBe(1);
  });

  it('refuses a file over the size limit', () => {
    const picked = screenImages([file('big.png', 'image/png', MAX_IMAGE_BYTES + 1)], 0);

    expect(picked.accepted).toEqual([]);
    expect(picked.errors).toEqual(['big.png is larger than 5 MB.']);
  });

  it('stops at the image count limit, counting what is already attached', () => {
    const files = [file('a.png', 'image/png'), file('b.png', 'image/png'), file('c.png', 'image/png')];

    const picked = screenImages(files, MAX_IMAGES - 1);

    expect(picked.accepted.length).toBe(1);
    expect(picked.errors).toEqual([`Only ${MAX_IMAGES} images can be attached to one message.`]);
  });
});

describe('screenImages edge cases', () => {
  it('does not treat a dotless file name as an extension', () => {
    const picked = screenImages([file('png', 'image/png')], 0);

    expect(picked.accepted).toEqual([]);
    expect(picked.errors).toEqual(['png is not a PNG, JPG, JPEG or WEBP image.']);
  });

  it('refuses an empty file by name', () => {
    const picked = screenImages([file('empty.png', 'image/png', 0)], 0);

    expect(picked.accepted).toEqual([]);
    expect(picked.errors).toEqual(['empty.png is empty.']);
  });
});

describe('newDraftId', () => {
  it('is a uuid-shaped id even where crypto.randomUUID is missing (plain http)', () => {
    const original = crypto.randomUUID;
    (crypto as { randomUUID?: unknown }).randomUUID = undefined;
    try {
      expect(newDraftId()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      expect(newDraftId()).not.toBe(newDraftId());
    } finally {
      crypto.randomUUID = original;
    }
  });
});

describe('toUpload', () => {
  it('sends the media type and the file bytes as base64', async () => {
    const upload = await toUpload(new File([new Uint8Array([1, 2, 3])], 'a.png', { type: 'image/png' }), 'image/png');

    expect(upload).toEqual({ name: 'a.png', media_type: 'image/png', data: 'AQID' });
  });
});
