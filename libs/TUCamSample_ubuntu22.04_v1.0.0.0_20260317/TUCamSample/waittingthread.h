#ifndef WAITTINGTHREAD_H
#define WAITTINGTHREAD_H

#include <QTime>
#include <QImage>
#include <QThread>

#include "./sdk/inc/TUCamApi.h"

class WaittingThread : public QThread
{
    Q_OBJECT

public:
    WaittingThread();
    ~WaittingThread();

    void stopThread();
    void startThread();

    bool isSaving() { return m_isSaving; }
    void stopSaving();
    void startSaving(const int &totalFrames, const int &intervalTime, const int &savedFormat);

    int getSrcWidth() { return m_frame.usWidth; }
    int getSrcHeight() { return m_frame.usHeight; }

signals:
    void signalSaving(int index);
    void signalSavingFinished();
    void signalUpdateImage(QImage img);
    void signalUpdateFrameRate(double m_frameRate);

protected:

    void run();

    bool saveImages();

    unsigned long getTickCount();

    void calculatedFrameRate();

private:

    bool m_isSaving;
    bool m_isWaitting;

    int m_totalFrames;
    int m_countFrames;
    int m_capturedFrames;
    int m_savedFormat;
    int m_intervalTime;
    int m_captureTimes;
    int m_moveBit;
    double m_frameRate;
    unsigned long m_startTicket;

    QTime m_time;
    HDTUCAM m_hIdxTUCam;
    TUCAM_FRAME m_frame;
};

#endif // WAITTINGTHREAD_H
