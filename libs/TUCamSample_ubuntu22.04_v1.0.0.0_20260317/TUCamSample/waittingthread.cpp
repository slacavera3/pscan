#include "waittingthread.h"

#include "camobject.h"

WaittingThread::WaittingThread() :
    m_isSaving(false),
    m_isWaitting(false),
    m_totalFrames(1),
    m_countFrames(0),
    m_capturedFrames(0),
    m_savedFormat(TUFMT_TIF),
    m_intervalTime(1000),
    m_captureTimes(0),
    m_frameRate(0),
    m_startTicket(0),
    m_moveBit(0),
    m_hIdxTUCam(NULL)
{

}

WaittingThread::~WaittingThread()
{

}

void WaittingThread::startThread()
{
    if (m_isWaitting)
        return;

    m_hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == m_hIdxTUCam)
    {
        return;
    }

    if (!this->isRunning())
    {
        m_frameRate = 0;
        m_countFrames = 0;
        m_isWaitting = true;
        m_startTicket = getTickCount();

        m_frame.pBuffer = NULL;
        m_frame.ucFormatGet = TUFRM_FMT_USUAl;
        m_frame.uiRsdSize = 1;

        m_moveBit = CamObject::getInstance()->getMoveBit();
        int triggerMode = CamObject::getInstance()->getTriggerMode();

        TUCAM_Buf_Alloc(m_hIdxTUCam, &m_frame);                         // Alloc buffer after set resolution or set ROI attribute
        TUCAM_Cap_Start(m_hIdxTUCam, triggerMode/*TUCCM_SEQUENCE*/);    // Start capture

        this->start();
    }
}

void WaittingThread::stopThread()
{
    if (NULL == m_hIdxTUCam)
    {
        return;
    }

    m_isWaitting = false;
    TUCAM_Buf_AbortWait(m_hIdxTUCam);

    if (this->isRunning())
    {
//      this->quit();
        this->wait();
    }

    TUCAM_Cap_Stop(m_hIdxTUCam);        // Stop capture
    TUCAM_Buf_Release(m_hIdxTUCam);     // Release alloc buffer after stop capture and quit drawing thread
}

void WaittingThread::stopSaving()
{
    m_isSaving = false;
}

void WaittingThread::startSaving(const int &totalFrames, const int &intervalTime, const int &savedFormat)
{
    m_isSaving = true;

    ++m_captureTimes;
    m_capturedFrames = 0;
    m_totalFrames = totalFrames;
    m_savedFormat = savedFormat;
    m_intervalTime = intervalTime;
}

void WaittingThread::run()
{
    if (NULL == m_hIdxTUCam)
    {
        return;
    }

    while (m_isWaitting)
    {
        m_frame.ucFormatGet = TUFRM_FMT_USUAl;

        TUCAMRET ret = TUCAM_Buf_WaitForFrame(m_hIdxTUCam, &m_frame);

 //       qDebug("after wait for frame:%d====\n", ret);

        if (TUCAMRET_SUCCESS == ret)
        {            
            int channels = m_frame.ucChannels;
            int width = m_frame.usWidth;
            int height = m_frame.usHeight;
            int elementBytes = m_frame.ucElemBytes;

            calculatedFrameRate();

            QImage img;

            if (3 == channels)
                img = QImage(width, height, QImage::Format_RGB888);
            else if (1 == channels)
                img = QImage(width, height, QImage::Format_Grayscale8);

            uchar *pSrc = (uchar *)m_frame.pBuffer + m_frame.usHeader;
            uchar *pDst = (uchar *)img.bits();

//            if (1 == elementBytes)
//            {
//                memcpy(pDst, pSrc, m_frame.uiImgSize);
//            }

            if (2 == elementBytes)
            {
                if (1 == channels)
                {
                    int pixels = width * height * channels;
                    ushort *pData16 = (ushort*)pSrc;
                    for (int i = 0; i < pixels; ++i)
                    {
                        ///*pDst++ = *pSrc;
                        ///pSrc += elementBytes;
                        *pDst++ = (uchar)((*pData16) >> m_moveBit);
                        pData16++;
                    }
                }
                else
                {
                    pSrc += (elementBytes / 2);
                    int pixels = width * height;
                    elementBytes *= 3;

                    for (int i = 0; i < pixels; ++i)
                    {
                        *pDst++ = *(pSrc+4);
                        *pDst++ = *(pSrc+2);
                        *pDst++ = *pSrc;
                        pSrc += elementBytes;
                    }
                }
            }
            else
            {
                if (1 == channels)
                    memcpy(pDst, pSrc, m_frame.uiImgSize);
                else
                {
                    int pixels = width * height;
                    elementBytes *= 3;

                    for (int i = 0; i < pixels; ++i)
                    {
                        *pDst++ = *(pSrc+2);
                        *pDst++ = *(pSrc+1);
                        *pDst++ = *pSrc;
                        pSrc += elementBytes;
                    }
                }
            }

            emit signalUpdateImage(img);

            if (m_isSaving)
            {
                saveImages();
            }
        }
    }
}

bool WaittingThread::saveImages()
{
    bool isSucceed = true;
    QString savedPath = CamObject::getInstance()->getSavedPath();

    do
    {
        if (0 == m_capturedFrames || m_time.elapsed() > m_intervalTime)
        {
            char fileName[1024];
            sprintf(fileName, "%s/ts_%d_%d", savedPath.toLocal8Bit().data(), m_captureTimes, (m_capturedFrames + 1));

            TUCAM_FILE_SAVE fs;
            fs.nSaveFmt = m_savedFormat;
            fs.pFrame = &m_frame;
            fs.pstrSavePath = fileName;

            int fmt = m_savedFormat;

            // Format RAW
            if (fmt & TUFMT_RAW)
            {
                fmt &= ~TUFMT_RAW;
            }

            if (0 != fmt)
            {
                fs.nSaveFmt = fmt;
            }

            if (TUCAMRET_SUCCESS != TUCAM_File_SaveImage(m_hIdxTUCam, fs))
            {
                isSucceed = false;
                break;
            }

            if (m_savedFormat & TUFMT_RAW)
            {
                fs.nSaveFmt = TUFMT_RAW;

                // Get Raw data
                m_frame.ucFormatGet = TUFRM_FMT_RAW;
                if (TUCAMRET_SUCCESS != TUCAM_Buf_CopyFrame(m_hIdxTUCam, &m_frame))
                {
                    isSucceed = false;
                    break;
                }

                // Save RAW data
                if (TUCAMRET_SUCCESS != TUCAM_File_SaveImage(m_hIdxTUCam, fs))
                {
                    isSucceed = false;
                    break;
                }
            }

            if (isSucceed)
            {
                ++m_capturedFrames;

                if (m_totalFrames > 1)
                {
                    m_time.restart();
                }

                emit signalSaving(m_capturedFrames);
            }
        }

    } while(0);

    if (m_capturedFrames >= m_totalFrames)
    {
        m_capturedFrames = 0;
        m_isSaving = false;
        emit signalSavingFinished();
    }

    return isSucceed;
}

unsigned long WaittingThread::getTickCount()
{
#ifdef Q_OS_LINUX
    struct timespec tm;

    clock_gettime(CLOCK_MONOTONIC, &tm);

    return (tm.tv_sec * 1000 + tm.tv_nsec / 1000000);
#else
    return GetTickCount();
#endif
}

void WaittingThread::calculatedFrameRate()
{
    m_countFrames++;
    unsigned long interval = getTickCount() - m_startTicket;

    if (interval > 1000)
    {
        m_frameRate = m_countFrames * 1000.0f / interval;
        m_startTicket = getTickCount();
        m_countFrames = 0;
        emit signalUpdateFrameRate(m_frameRate);
    }
}
