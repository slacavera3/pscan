#ifndef CAMOBJECT_H
#define CAMOBJECT_H

#include <QString>
#include <omp.h>
#include "waittingthread.h"
#include "./sdk/inc/TUCamApi.h"

const int PID_MICHROME6    = 0xEC09;
const int PID_MICHROME20   = 0xEC0B;
const int PID_MICHROME5PRO = 0xEC07;
const int PID_FL26BW         = 0xE423;
const int PID_FL9BW          = 0xE422;
const int PID_FL9BWLT        = 0xE426;
const int PID_LIBRA16        = 0xE435;
const int PID_LIBRA22        = 0xE436;
const int PID_LIBRA25        = 0xE437;

class CamObject
{    
public:
    ~CamObject();

    static CamObject *getInstance();
    int getCamPID() { return m_pid; }
    QString getSavedPath() { return m_savedPath; }
    HDTUCAM getCamHandle() { return  m_opCam.hIdxTUCam; }
    WaittingThread *getWaittingThread() { return &m_waittingThread; }

    void stopWaittingForFrames();
    void startWaittingForFrames();

    uint getDelayTime() { return m_delayTime; }
    void setDelayTime(uint delayTime = 0) { m_delayTime = delayTime; }

    int getTriggerMode() { return m_triggerMode; }
    void setTriggerMode(int mode) { m_triggerMode = mode; }

    int getTriggerExp() { return m_triggerExp; }
    void setTriggerExp(int exp) { m_triggerExp = exp; }

    int getTriggerEdge() { return m_triggerEdge; }
    void setTriggerEdge(int edge) { m_triggerEdge = edge; }

    void setSavedPath(const QString &path) { m_savedPath = path; }
    void stopSavingImages();
    void startSavingImages(const int &totalFrames, const int &intervalTime, const int &savedFormat);

    int getSrcWidth() { return m_waittingThread.getSrcWidth(); }
    int getSrcHeight() { return m_waittingThread.getSrcHeight(); }
    int getMoveBit();

    bool isSaving() { return m_waittingThread.isSaving(); }
    bool isWaitting() { return m_waittingThread.isRunning(); }

    static TUCAM_INIT s_itCam;
    static CamObject *s_instance;
    static QString m_configPath;

    class GarbageCollector
    {
        public:
        ~GarbageCollector()
        {
            if (NULL != CamObject::s_instance)
            {
                delete CamObject::s_instance;
                CamObject::s_instance = NULL;
            }
        }
    };
    static GarbageCollector gc;

    bool  isSupportFL9BW()   {return m_pid == PID_FL9BW;}
    bool  isSupportFL9BWLT() {return m_pid == PID_FL9BWLT;}
    bool  isSupportFL26BW()  {return m_pid == PID_FL26BW;}
    bool  isSupportLibra16() {return m_pid == PID_LIBRA16;}
    bool  isSupportLibra22() {return m_pid == PID_LIBRA22;}
    bool  isSupportLibra25() {return m_pid == PID_LIBRA25;}

private:
    CamObject();

    int m_pid;
    TUCAM_OPEN m_opCam;
    WaittingThread m_waittingThread;

    int m_triggerMode;
    int m_triggerExp;
    int m_triggerEdge;
    uint m_delayTime;

    QString m_savedPath;
};

#endif // CAMOBJECT_H
