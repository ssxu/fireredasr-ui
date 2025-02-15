HOST='127.0.0.1'
PORT=5078
BATCH=2
from flask import Flask, request, jsonify,send_file,Response,render_template
from pathlib import Path
import os,random,sys,threading, webbrowser, time,datetime,hashlib,tempfile,logging,subprocess,torch,glob,re
from waitress import serve
from flask_cors import CORS
from datetime import timedelta
from faster_whisper.audio import decode_audio
from faster_whisper.vad import (
    VadOptions,
    get_speech_timestamps
)
from pydub import AudioSegment
from fireredasr.models.fireredasr import FireRedAsr

ROOT_DIR=Path(__file__).parent.as_posix()
if sys.platform == 'win32':
    os.environ['PATH'] = ROOT_DIR + f';{ROOT_DIR}/ffmpeg;' + os.environ['PATH']

STATIC_DIR=f'{ROOT_DIR}/static'
LOGS_DIR=f'{ROOT_DIR}/logs'
TMP_DIR=f'{STATIC_DIR}/tmp'

Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_file_handler = logging.FileHandler(f'{LOGS_DIR}/{datetime.datetime.now().strftime("%Y%m%d")}.log', encoding='utf-8')
_file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
_file_handler.setFormatter(formatter)
logger.addHandler(_file_handler)


app = Flask(__name__, template_folder=f'{ROOT_DIR}/templates')
CORS(app)

# 将字符串做 md5 hash处理
def get_md5(input_string):
    md5 = hashlib.md5()
    md5.update(input_string.encode('utf-8'))
    return md5.hexdigest()

def runffmpeg(cmd):
    
    try:
        if cmd[0]!='ffmpeg':
            cmd.insert(0,'ffmpeg')
        logger.info(f'{cmd=}')
        subprocess.run(cmd,
                       stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,
                       encoding="utf-8",
                       check=True,
                       text=True,
                       creationflags=0 if sys.platform != 'win32' else subprocess.CREATE_NO_WINDOW)

    except Exception as e:
        raise Exception(str(e.stderr) if hasattr(e, 'stderr') and e.stderr else f'执行Ffmpeg操作失败:{cmd=}')
    return True

'''
格式化毫秒或秒为符合srt格式的 2位小时:2位分:2位秒,3位毫秒 形式
print(ms_to_time_string(ms=12030))
-> 00:00:12,030
'''


def ms_to_time_string(*, ms=0, seconds=None):
    # 计算小时、分钟、秒和毫秒
    if seconds is None:
        td = timedelta(milliseconds=ms)
    else:
        td = timedelta(seconds=seconds)
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    milliseconds = td.microseconds // 1000

    time_string = f"{hours}:{minutes}:{seconds},{milliseconds}"
    return format_time(time_string, ',')


# 将不规范的 时:分:秒,|.毫秒格式为  aa:bb:cc,ddd形式
# eg  001:01:2,4500  01:54,14 等做处理
def format_time(s_time="", separate=','):
    if not s_time.strip():
        return f'00:00:00{separate}000'
    hou, min, sec, ms = 0, 0, 0, 0

    tmp = s_time.strip().split(':')
    if len(tmp) >= 3:
        hou, min, sec = tmp[-3].strip(), tmp[-2].strip(), tmp[-1].strip()
    elif len(tmp) == 2:
        min, sec = tmp[0].strip(), tmp[1].strip()
    elif len(tmp) == 1:
        sec = tmp[0].strip()

    if re.search(r',|\.', str(sec)):
        t = re.split(r',|\.', str(sec))
        sec = t[0].strip()
        ms = t[1].strip()
    else:
        ms = 0
    hou = f'{int(hou):02}'[-2:]
    min = f'{int(min):02}'[-2:]
    sec = f'{int(sec):02}'
    ms = f'{int(ms):03}'[-3:]
    return f"{hou}:{min}:{sec}{separate}{ms}"

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(app.config['STATIC_FOLDER'], filename)


@app.route('/')
def index():
    return render_template(  'index.html' )

@app.route('/v1/audio/translations', methods=['POST'])
def uploadfile():
    try:
        if 'file' not in request.files:  # 检查是否上传了文件
            return jsonify({"code": 1, 'error': 'No file part'}),500

        file = request.files['file']
        if file.filename == '':  # 检查是否选择了文件
            return jsonify({"code": 1, 'error': 'No selected file'}),500
        response_format=request.form.get('response_format','srt')
        
        # 获取文件扩展名
        # 使用时间戳生成文件名
        name=f'{time.time()}'
        ext=os.path.splitext(file.filename)[1]
        filename_raw = f'{TMP_DIR}/raw-{name}{ext}'
        filename_16k = f'{TMP_DIR}/16k-{name}.wav'
        # 创建目录
        target_dir = f'{TMP_DIR}/{name}'
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        file.save(filename_raw)
        # 保存文件到 /tmp 目录
        runffmpeg(['-y', '-i', filename_raw,'-ac','1','-ar','16000', '-c:a', 'pcm_s16le','-f','wav', filename_16k])
        wavs=cut_audio(filename_16k,target_dir)
        srts=asr_task(wavs,asr_type='aed')
        if response_format == 'text':
            return Response(". ".join([it['text'] for it in srts]), mimetype='text/plain')
        if response_format=='json':
            return jsonify({"text":". ".join([it['text'] for it in srts])})
        result=[f"{it['line']}\n{it['startraw']} --> {it['endraw']}\n{it['text']}" for it in srts]
        return Response("\n\n".join(result), mimetype='text/plain')
    except Exception as e:
        return jsonify({"code": 1, 'error': str(e)}),500

def asr_task(wavs,asr_type='aed'):
    model = FireRedAsr.from_pretrained(asr_type, f'{ROOT_DIR}/pretrained_models/FireRedASR-AED-L' if asr_type=='aed' else f'{ROOT_DIR}/pretrained_models/FireRedASR-LLM-L')
    
    idxs={}
    for i,it in enumerate(wavs):
        idxs[it['uttid']]=i
    wavs_chunks=[wavs[i:i+BATCH] for i in range(0,len(wavs),BATCH)]
    for it in wavs_chunks:
        results = model.transcribe(
            [em['uttid'] for em in it],
            [em['file'] for em in it],
            {
            "use_gpu": 1 if torch.cuda.is_available() else 0,
            "beam_size": 1,
            "nbest": 1,
            "decode_max_len": 0,
            "softmax_smoothing": 1.0,
            "aed_length_penalty": 0.0,
            "eos_penalty": 1.0,
            "decode_min_len": 0,
            "repetition_penalty": 1.0,
            "llm_length_penalty": 0.0,
            "temperature": 1.0
            }
        )
        print(results)
        for result in results:
            wavs[idxs[result['uttid']]]['text']=result['text']

    return wavs


def openurl(url):
    def op():
        time.sleep(5)
        try:

            webbrowser.open_new_tab(url)
        except:
            pass

    threading.Thread(target=op).start()


# 根据 时间开始结束点，切割音频片段,并保存为wav到临时目录，记录每个wav的绝对路径到list，然后返回该list
def cut_audio(audio_file,dir_name):
    sampling_rate=16000
    

    def convert_to_milliseconds(timestamps):
        milliseconds_timestamps = []
        for timestamp in timestamps:
            milliseconds_timestamps.append(
                {
                    "start": int(round(timestamp["start"] / sampling_rate * 1000)),
                    "end": int(round(timestamp["end"] / sampling_rate * 1000)),
                }
            )

        return milliseconds_timestamps
    vad_p={
        "threshold":  0.5,
        "neg_threshold": 0.35,
        "min_speech_duration_ms":  0,
        "max_speech_duration_s":  float("inf"),
        "min_silence_duration_ms": 250,
        "speech_pad_ms": 200
    }
    speech_chunks=get_speech_timestamps(decode_audio(audio_file, sampling_rate=sampling_rate),vad_options=VadOptions(**vad_p))
    speech_chunks=convert_to_milliseconds(speech_chunks)
    
    data=[]
    audio = AudioSegment.from_wav(audio_file)
    for i,it in enumerate(speech_chunks):
        start_ms, end_ms=it['start'],it['end']
        chunk = audio[start_ms:end_ms]
        file_name=f"{dir_name}/{start_ms}_{end_ms}.wav"
        chunk.export(file_name, format="wav")
        data.append({
        "line":i+1,
        "start_time":start_ms,
        "end_time":end_ms,
        "file":file_name,
        "text":"",
        'uttid':f'{start_ms}_{end_ms}',
        'startraw':ms_to_time_string(ms=start_ms),
        'endraw':ms_to_time_string(ms=end_ms),
        })

    return data
    
if __name__ == '__main__':
    try:
        print(f"api接口地址  http://{HOST}:{PORT}")
        openurl(f'http://{HOST}:{PORT}')
        serve(app, host=HOST, port=PORT,threads=4)
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        logger.error(traceback.format_exc())
    