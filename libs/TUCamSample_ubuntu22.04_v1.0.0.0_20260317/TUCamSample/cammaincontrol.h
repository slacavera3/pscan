#ifndef CAMMAINCONTROL_H
#define CAMMAINCONTROL_H

#include <QWidget>

#include "camobject.h"

namespace Ui {
class CamMainControl;
}

class CamMainControl : public QWidget
{
    Q_OBJECT

public:
    explicit CamMainControl(QWidget *parent = 0);
    ~CamMainControl();

    void initRange();
    void updateValue();
    void updateExposureTime(uint exposureTime);
    void updateAutoExposureState();

signals:
    void signalUpdateLevelRange();

protected:
    void paintEvent(QPaintEvent *event);

    void timerEvent(QTimerEvent *event);

public slots:
    void slotUpdateFrameRate(double frameRate);

private slots:
    void on_pbLive_clicked();

    void on_cbbResolution_currentIndexChanged(int index);

    void on_cbbBin_currentIndexChanged(int index);

    void on_hsldGrian_valueChanged(int value);

    void on_hsldBrightness_valueChanged(int value);

    void on_cbAE_clicked(bool checked);

    void on_pbOk_clicked();

    void on_cmbShutter_currentIndexChanged(int index);

    void on_cmbMode_currentIndexChanged(int index);

    void on_horizontalSliderCooling_valueChanged(int value);

    void on_pushButtonFrameRate_clicked();

    void on_pushButtonMaxAutoExp_clicked();

    void on_checkBoxLed_clicked(bool checked);

private:
    Ui::CamMainControl *ui;

    bool m_isSupportMs;
    bool m_isSupportSec;
    bool m_isSupportAeTarget;

    int m_aeTimerId;
    int m_tempTimerId;
    int m_midTemp;

    TUCAM_PROP_ATTR m_frameRateProp;
    TUCAM_PROP_ATTR m_maxAEProp;
    TUCAM_PROP_ATTR m_gainProp;
    TUCAM_PROP_ATTR m_aeTargetProp;
    TUCAM_PROP_ATTR m_exposureProp;
    TUCAM_PROP_ATTR m_temperatureProp;

    TUCAM_CAPA_ATTR m_modeCapa;
    TUCAM_CAPA_ATTR m_shutterCapa;

    TUCAM_CAPA_ATTR m_aeCapa;
    TUCAM_CAPA_ATTR m_resolutionCapa;
    TUCAM_CAPA_ATTR m_binCapa;

};

#endif // CAMMAINCONTROL_H
