/**
 * FacesPanel — the tab had no test at all until 2026-09-15.
 *
 * WHY IT NEEDED ONE, and why these particular assertions. The faces gallery
 * shipped 09-14 with every thumbnail blank, from two independent causes, and
 * BOTH of them lived on paths a screenshot confirms and nothing else does:
 *
 *   1. an `<img src="/api/search/image?...">` cannot carry a bearer token, so
 *      the crop came back 401 and rendered as an empty tile. The fix is to go
 *      through `CropThumb`, which fetches with the token and hands the element
 *      an object URL.
 *   2. the crop proxy filtered `("person","vehicles")`, so `domain=face` was a
 *      404 behind that same tile.
 *
 * So the first test below asserts the two things that were wrong: that the
 * gallery renders through CropThumb, and that it asks for domain="face". The
 * proxy half is pinned on the server in
 * `services/camera-mgmt/tests/test_crop_proxy_domains.py`; this is the browser
 * half, and neither is any use without the other.
 *
 * THE OTHER THEME IS THAT THIS TAB'S NUMBERS DO NOT MEAN WHAT THEY LOOK LIKE.
 * Measured on this product's own footage: the same person in two photographs
 * scores median 0.387 and two different people 0.117, so 0.4 is a STRONG match
 * here while reading as a failure anywhere else in the UI. The word beside the
 * number is the feature, not decoration, and it is asserted as such.
 *
 * MOCKING NOTE. `@/lib/smartsearch` is mocked rather than the fetch layer: the
 * request-building in that module has its own contract and re-testing it here
 * would pin the same URLs twice and make both harder to change. What this file
 * is about is what the PANEL does with the answers.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FaceHit, SearchCamera } from '@/lib/smartsearch';

const recentFaces = vi.fn();
const searchFacesByImage = vi.fn();

vi.mock('@/lib/smartsearch', () => ({
  recentFaces: (...a: any[]) => recentFaces(...a),
  searchFacesByImage: (...a: any[]) => searchFacesByImage(...a),
}));

/** Stand-in for the authenticated thumbnail. Renders what it was ASKED for, so
 *  a test can assert the domain and id rather than the pixels. */
vi.mock('./ResultCard', () => ({
  CropThumb: ({ domain, id }: { domain: string; id: string }) => (
    <img data-testid="crop-thumb" data-domain={domain} data-id={id} alt="" />
  ),
}));

vi.mock('./EventClipModal', () => ({
  EventClipModal: () => <div data-testid="event-clip" />,
}));

import { FacesPanel } from './FacesPanel';

const CAM: SearchCamera = { id: 'c1', slug: 'exit-gate-hvte', name: 'Exit gate' };

function hit(over: Partial<FaceHit> = {}): FaceHit {
  return {
    id: '121',
    camera: CAM,
    camera_id: 'exit-gate-hvte',
    when_ms: Date.UTC(2026, 8, 15, 5, 38, 13),
    face_width_px: 70,
    ...over,
  };
}

// jsdom implements neither of these. The panel shows the uploaded photo beside
// the results — "searching for THIS face" — and that needs an object URL for a
// File the test never really reads. Stubbed rather than worked around, so the
// preview path runs in the test exactly as it does in the browser.
let objectUrlCount = 0;
const revokeObjectURL = vi.fn();
beforeAll(() => {
  URL.createObjectURL = (() => `blob:mock/${objectUrlCount++}`) as typeof URL.createObjectURL;
  URL.revokeObjectURL = revokeObjectURL as typeof URL.revokeObjectURL;
});

beforeEach(() => {
  recentFaces.mockReset();
  searchFacesByImage.mockReset();
  revokeObjectURL.mockClear();
  recentFaces.mockResolvedValue({ results: [], total: 0 });
  objectUrlCount = 0;
});

function uploadPhoto(name = 'face.jpg') {
  return userEvent.upload(
    screen.getByLabelText(/Photo/i) as HTMLInputElement,
    new File([name], name, { type: 'image/jpeg' }),
  );
}

describe('the uploaded photo is released, not leaked', () => {
  // Every search makes an object URL for the photo shown beside the results,
  // and until 2026-09-15 none was ever revoked: one blob held for the life of
  // the tab per search. Each case below is a way the preview stops being shown.
  beforeEach(() => {
    searchFacesByImage.mockResolvedValue({ results: [], total: 0, faces_detected: 1 });
  });

  it('when a second search replaces it — and not the one now on screen', async () => {
    render(<FacesPanel cameras={[CAM]} />);
    await uploadPhoto('first.jpg');
    await screen.findByText(/nothing in the index is close/i);
    expect(revokeObjectURL).not.toHaveBeenCalled();

    await uploadPhoto('second.jpg');
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock/0'));
    expect(revokeObjectURL).not.toHaveBeenCalledWith('blob:mock/1');
  });

  it('when the search is cleared', async () => {
    render(<FacesPanel cameras={[CAM]} />);
    await uploadPhoto();
    await userEvent.click(await screen.findByRole('button', { name: /Clear search/i }));
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock/0');
  });

  it('when the tab is left', async () => {
    const { unmount } = render(<FacesPanel cameras={[CAM]} />);
    await uploadPhoto();
    await screen.findByText(/nothing in the index is close/i);
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock/0');
  });
});

describe('the gallery', () => {
  it('fetches every crop through the authenticated thumbnail, as domain "face"', async () => {
    // BOTH HALVES OF THE BLANK-GALLERY BUG, in one assertion each. A plain
    // <img src> would be a 401; domain="person" would be a 404 at the proxy.
    recentFaces.mockResolvedValue({ results: [hit(), hit({ id: '120' })], total: 2 });
    render(<FacesPanel cameras={[CAM]} />);

    const thumbs = await screen.findAllByTestId('crop-thumb');
    expect(thumbs).toHaveLength(2);
    expect(thumbs.map(t => t.getAttribute('data-domain'))).toEqual(['face', 'face']);
    expect(thumbs.map(t => t.getAttribute('data-id'))).toEqual(['121', '120']);
  });

  it('explains an empty gallery instead of showing a blank grid', async () => {
    // An empty screen cannot distinguish "no faces here" from "the feature is
    // off for this camera" — and on a feature whose honest yield is ~4% of
    // passes, "empty" is exactly what a working install looks like at first.
    render(<FacesPanel cameras={[CAM]} />);
    expect(await screen.findByText(/No faces recorded yet/i)).toBeTruthy();
    expect(screen.getByText(/switched on for it in AI/i)).toBeTruthy();
  });

  it('shows no score on a gallery listing, because nothing has been compared', async () => {
    recentFaces.mockResolvedValue({ results: [hit({ score: null })], total: 1 });
    render(<FacesPanel cameras={[CAM]} />);

    await screen.findByTestId('crop-thumb');
    expect(screen.queryByText(/strong|possible|weak/)).toBeNull();
  });
});

describe('the score is labelled, because 0.4 is a good match on this scale', () => {
  it.each([
    [0.974, 'strong'],   // the verified self-match
    [0.45, 'strong'],    // the band edge itself
    [0.387, 'possible'], // measured median for the SAME person
    [0.117, 'weak'],     // measured median for DIFFERENT people
  ])('%s reads as "%s"', async (score, word) => {
    searchFacesByImage.mockResolvedValue({
      results: [hit({ score })], total: 1, faces_detected: 1,
    });
    render(<FacesPanel cameras={[CAM]} />);

    await userEvent.upload(
      screen.getByLabelText(/Photo/i) as HTMLInputElement,
      new File(['x'], 'face.jpg', { type: 'image/jpeg' }),
    );

    const card = (await screen.findAllByTestId('crop-thumb'))[0].closest('figure')!;
    expect(within(card).getByText(score.toFixed(3))).toBeTruthy();
    expect(within(card).getByText(word)).toBeTruthy();
  });
});

describe('the two ways a search comes back empty are different problems', () => {
  it('says the PHOTO had no face in it, rather than "no matches"', async () => {
    // THE ONE FAILURE THE OPERATOR CAN FIX. "No matches" sends them looking for
    // a person who may be standing in front of the camera; the truth is their
    // photograph had no findable face.
    searchFacesByImage.mockResolvedValue({
      results: [], total: 0, faces_detected: 0,
      detail: 'No face was found in that photo.',
    });
    render(<FacesPanel cameras={[CAM]} />);

    await userEvent.upload(
      screen.getByLabelText(/Photo/i) as HTMLInputElement,
      new File(['x'], 'landscape.jpg', { type: 'image/jpeg' }),
    );

    expect(await screen.findByText(/No face was found in that photo/i)).toBeTruthy();
  });

  it('says the INDEX had nothing close, when the photo was fine', async () => {
    searchFacesByImage.mockResolvedValue({
      results: [], total: 0, faces_detected: 1,
    });
    render(<FacesPanel cameras={[CAM]} />);

    await userEvent.upload(
      screen.getByLabelText(/Photo/i) as HTMLInputElement,
      new File(['x'], 'face.jpg', { type: 'image/jpeg' }),
    );

    expect(await screen.findByText(/nothing in the index is close to it/i)).toBeTruthy();
  });
});

describe('the filters reach the index rather than only the screen', () => {
  it('passes the camera and the minimum face size to the gallery query', async () => {
    // The size floor is a quality gate with a measured basis — rank-1 was 24%
    // below 40px against 40% at 64-111px — so filtering client-side over a
    // 60-row page would silently answer a different question.
    render(<FacesPanel cameras={[CAM]} />);
    await waitFor(() => expect(recentFaces).toHaveBeenCalled());

    await userEvent.selectOptions(screen.getByLabelText(/Camera/i), 'exit-gate-hvte');
    await userEvent.selectOptions(screen.getByLabelText(/Minimum face size/i), '64');

    await waitFor(() => {
      expect(recentFaces).toHaveBeenLastCalledWith(
        expect.objectContaining({ camera: 'exit-gate-hvte', min_width_px: 64 }),
      );
    });
  });

  it('carries the same filters into a photo search', async () => {
    searchFacesByImage.mockResolvedValue({ results: [], total: 0, faces_detected: 1 });
    render(<FacesPanel cameras={[CAM]} />);
    await waitFor(() => expect(recentFaces).toHaveBeenCalled());

    await userEvent.selectOptions(screen.getByLabelText(/Minimum face size/i), '112');
    await userEvent.upload(
      screen.getByLabelText(/Photo/i) as HTMLInputElement,
      new File(['x'], 'face.jpg', { type: 'image/jpeg' }),
    );

    await waitFor(() => {
      expect(searchFacesByImage).toHaveBeenCalledWith(
        [expect.any(File)],
        expect.objectContaining({ min_width_px: 112 }),
      );
    });
  });
});

describe('several photos of one person', () => {
  function uploadPhotos(...names: string[]) {
    return userEvent.upload(
      screen.getByLabelText(/Photo/i) as HTMLInputElement,
      names.map(n => new File([n], n, { type: 'image/jpeg' })),
    );
  }

  it('sends every selected photo in ONE search, in the order chosen', async () => {
    searchFacesByImage.mockResolvedValue({ results: [], total: 0, faces_detected: 2 });
    render(<FacesPanel cameras={[CAM]} />);
    await uploadPhotos('front.jpg', 'side.jpg');

    await waitFor(() => expect(searchFacesByImage).toHaveBeenCalledTimes(1));
    const [files] = searchFacesByImage.mock.calls[0];
    expect((files as File[]).map(f => f.name)).toEqual(['front.jpg', 'side.jpg']);
  });

  it('refuses more than five before uploading anything', async () => {
    render(<FacesPanel cameras={[CAM]} />);
    await uploadPhotos('1.jpg', '2.jpg', '3.jpg', '4.jpg', '5.jpg', '6.jpg');

    // The count, not "up to 5": the hint under the picker says that too, and
    // would pass this assertion with no error shown at all.
    expect(await screen.findByText(/6 were selected/i)).toBeTruthy();
    expect(searchFacesByImage).not.toHaveBeenCalled();
  });

  it('marks the photo in which no face was found, and counts only the ones used', async () => {
    // Otherwise a set of three where one photo was useless reads as a search
    // over three, and the operator cannot tell which to replace.
    searchFacesByImage.mockResolvedValue({
      results: [hit({ score: 0.5 })], total: 1, faces_detected: 2,
      photos: [
        { photo: 1, faces_detected: 1, used: true },
        { photo: 2, faces_detected: 0, used: false },
        { photo: 3, faces_detected: 1, used: true },
      ],
      photos_used: 2, agreement: 0.41,
    });
    render(<FacesPanel cameras={[CAM]} />);
    await uploadPhotos('a.jpg', 'b.jpg', 'c.jpg');

    const unused = await screen.findAllByText(/No face found — not used/i);
    expect(unused).toHaveLength(1);
    expect(unused[0].closest('figure')!.querySelector('img')!.getAttribute('alt'))
      .toBe('Search photo 2');
    expect(screen.getByText(/across 2 of 3 photos/i)).toBeTruthy();
  });

  it.each([
    [0.117, true],   // measured median for DIFFERENT people
    [0.249, true],   // just under the band edge
    [0.25, false],   // the band edge itself
    [0.387, false],  // measured median for the SAME person
  ])('agreement %s warns that the photos may be different people: %s', async (agreement, warns) => {
    // The same WEAK band the match scores use, because it is the same scale:
    // below it, two faces are indistinguishable from unrelated ones.
    searchFacesByImage.mockResolvedValue({
      results: [hit({ score: 0.3 })], total: 1, faces_detected: 2, agreement,
    });
    render(<FacesPanel cameras={[CAM]} />);
    await uploadPhotos('a.jpg', 'b.jpg');
    await screen.findByTestId('crop-thumb');

    expect(screen.queryByText(/may not all show the same person/i) !== null).toBe(warns);
  });

  it('releases every preview of a replaced set — and none of the new one', async () => {
    searchFacesByImage.mockResolvedValue({ results: [], total: 0, faces_detected: 1 });
    render(<FacesPanel cameras={[CAM]} />);
    await uploadPhotos('a.jpg', 'b.jpg');
    await screen.findByText(/nothing in the index is close/i);

    await uploadPhotos('c.jpg');
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock/1'));
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock/0');
    expect(revokeObjectURL).not.toHaveBeenCalledWith('blob:mock/2');
  });
});

describe('failure is reported, not swallowed', () => {
  it('shows the error when the gallery cannot be loaded', async () => {
    recentFaces.mockRejectedValue(new Error('Smart Search is not configured'));
    render(<FacesPanel cameras={[CAM]} />);
    expect(await screen.findByText(/Smart Search is not configured/i)).toBeTruthy();
  });

  it('shows the error when a search fails, and does not leave stale hits on screen', async () => {
    recentFaces.mockResolvedValue({ results: [hit()], total: 1 });
    searchFacesByImage.mockRejectedValue(new Error('index unreachable'));
    render(<FacesPanel cameras={[CAM]} />);
    await screen.findByTestId('crop-thumb');

    await userEvent.upload(
      screen.getByLabelText(/Photo/i) as HTMLInputElement,
      new File(['x'], 'face.jpg', { type: 'image/jpeg' }),
    );

    expect(await screen.findByText(/index unreachable/i)).toBeTruthy();
    expect(screen.queryByTestId('crop-thumb')).toBeNull();
  });
});
