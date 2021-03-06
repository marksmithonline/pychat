import {JsAudioAnalyzer} from '../types/types';
import {extractError} from './utils';
import {globalLogger, isMobile} from './singletons';
import {IS_DEBUG } from './consts';

let audioContext: AudioContext;
let audioProcesssors = [];
if (IS_DEBUG) {
  window['audioProcesssors'] = audioProcesssors;
}

export function createMicrophoneLevelVoice (stream: MediaStream, onaudioprocess: Function): JsAudioAnalyzer {
  try {
    if (isMobile) {
      globalLogger.log('Current phone is mobile, audio processor won\'t be created')();
      return;
    }
    let audioTracks: MediaStreamTrack[] = stream && stream.getAudioTracks();
    if (audioTracks.length === 0) {
      throw 'Stream has no audio tracks';
    }
    let audioTrack: MediaStreamTrack = audioTracks[0];
    if (!audioContext) {
      audioContext = new AudioContext();
    }
    let analyser = audioContext.createAnalyser();
    let microphone = audioContext.createMediaStreamSource(stream);
    let javascriptNode = audioContext.createScriptProcessor(2048, 1, 1);
    analyser.smoothingTimeConstant = 0.3;
    analyser.fftSize = 1024;
    microphone.connect(analyser);
    analyser.connect(javascriptNode);
    javascriptNode.connect(audioContext.destination);
    let prevVolumeValues = 0;
    let volumeValuesCount = 0;
    let res: JsAudioAnalyzer = {
      analyser,
      javascriptNode,
      prevVolumeValues,
      volumeValuesCount
    };
    javascriptNode.onaudioprocess = onaudioprocess(res);
    audioProcesssors.push(res);
    globalLogger.log('Created new audioProcessor')();
    return res;
  } catch (err) {
    globalLogger.error('Unable to use microphone level because {}', extractError(err))();
    return null;
  }
}

export function getAverageAudioLevel (audioProc: JsAudioAnalyzer) {
  let array = new Uint8Array(audioProc.analyser.frequencyBinCount);
  audioProc.analyser.getByteFrequencyData(array);
  let values = 0;
  let length = array.length;
  for (let i = 0; i < length; i++) {
    values += array[i];
  }
  return values / length;
}