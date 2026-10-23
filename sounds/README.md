# 启动音效

把本地音效保存为 `startup.wav`，或在 `config.yaml` 中修改 `STARTUP_SOUND_FILE`。
音频需要为 16 位 PCM WAV，支持单声道、双声道，最长 15 秒。
音频文件作为设备素材保存在本地，不随源码提交。

当前设备使用《红色警戒2》自爆卡车的 **Watch out for the bumps!**（`Vdemmoc`）。
音效来源：[心灵终结中文 Wiki 的自爆卡车语音表](https://moapyr.fandom.com/zh/wiki/%E8%87%AA%E7%88%86%E5%8D%A1%E8%BD%A6#%E8%AF%AD%E9%9F%B3)。游戏音频版权属于其原权利人。

如果原文件为 OGG、MP3 等格式，可在安装 FFmpeg 的机器转换后放入本目录：

```bash
ffmpeg -i Vdemmoc.ogg -ac 1 -ar 48000 -c:a pcm_s16le startup.wav
```

服务正常运行只依赖 ALSA 播放，不需要 FFmpeg 或在线下载。
