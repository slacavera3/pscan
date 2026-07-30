#include "camobject.h"

#include <QStandardPaths>

TUCAM_INIT CamObject::s_itCam;
QString CamObject::m_configPath = "";
CamObject *CamObject::s_instance = NULL;
CamObject::GarbageCollector CamObject::gc;

CamObject *CamObject::getInstance()
{
    if (NULL == s_instance)
    {
        s_instance = new CamObject();
    }

    return s_instance;
}

CamObject::CamObject()
{
    m_configPath = QStandardPaths::writableLocation(QStandardPaths::DesktopLocation);

    m_triggerMode = TUCCM_SEQUENCE;
    m_triggerExp  = TUCTE_EXPTM;
    m_triggerEdge = TUCTD_FAILING;
    m_delayTime   = 0;

    s_itCam.pstrConfigPath = "./"/*m_configPath.toLocal8Bit().data()*/;
    s_itCam.uiCamCount = 0;

    m_savedPath = m_configPath + "/Image";

    if (TUCAMRET_SUCCESS == TUCAM_Api_Init(&s_itCam))
    {
        if (s_itCam.uiCamCount > 0)
        {
            m_opCam.hIdxTUCam = NULL;
            m_opCam.uiIdxOpen = 0;

            // Open camera
            TUCAM_Dev_Open(&m_opCam);

            // Get camera PID
            TUCAM_VALUE_INFO valueInfo;
            valueInfo.nID = TUIDI_CAMERA_MODEL;
            valueInfo.nTextSize = 64;
            valueInfo.nValue = 0;
            valueInfo.pText = NULL;
            valueInfo.nID = TUIDI_PRODUCT;
            if (TUCAMRET_SUCCESS == TUCAM_Dev_GetInfo(m_opCam.hIdxTUCam, &valueInfo))
            {
                m_pid = valueInfo.nValue;
            }
        }
    }
}

CamObject::~CamObject()
{
    m_waittingThread.stopThread();

    if (NULL != m_opCam.hIdxTUCam)
    {
        TUCAM_Cap_Stop(m_opCam.hIdxTUCam);
        TUCAM_Dev_Close(m_opCam.hIdxTUCam);
    }

    TUCAM_Api_Uninit();
}

void CamObject::stopWaittingForFrames()
{
    m_waittingThread.stopThread();
}

void CamObject::startWaittingForFrames()
{
    m_waittingThread.startThread();
}

void CamObject::stopSavingImages()
{
    m_waittingThread.stopSaving();
}

void CamObject::startSavingImages(const int &totalFrames, const int &intervalTime, const int &savedFormat)
{
    m_waittingThread.startSaving(totalFrames, intervalTime, savedFormat);
}

int CamObject::getMoveBit()
{
    int moveBit = 0;
    TUCAM_VALUE_INFO valueInfo;
    valueInfo.nID = TUIDI_VALID_FRAMEBIT;
    valueInfo.nTextSize = 64;
    valueInfo.nValue = 0;
    valueInfo.pText = NULL;
    if (TUCAMRET_SUCCESS != TUCAM_Dev_GetInfo(m_opCam.hIdxTUCam, &valueInfo))
    {
        return moveBit;
    }

    moveBit = valueInfo.nValue - 8;

    return moveBit;
}
