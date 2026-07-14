/**
 * Requests microphone permission from a real extension page.
 *
 * See index.html for why this page has to exist: an offscreen document can use
 * the microphone but cannot prompt for it, so the grant must be obtained here
 * first. It is granted to the extension origin, so the offscreen document
 * inherits it.
 */

const button = document.getElementById('grant');

button?.addEventListener('click', () => {
  void navigator.mediaDevices
    .getUserMedia({ audio: true })
    .then((stream) => {
      // We only wanted the permission, not the audio. Release the device again
      // so the recorder can take it cleanly.
      for (const track of stream.getTracks()) track.stop();
      window.close();
    })
    .catch((err: unknown) => {
      console.error('[permission] microphone denied', err);
      const message = document.createElement('p');
      message.textContent =
        'Microphone access was denied. Without it, your own voice will be missing from the transcript.';
      document.body.appendChild(message);
    });
});
