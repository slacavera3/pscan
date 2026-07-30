#ifndef CAMTRIGGER_H
#define CAMTRIGGER_H

#include <QWidget>

#include "camobject.h"

namespace Ui {
class CamTrigger;
}

class CamTrigger : public QWidget
{
    Q_OBJECT

public:
    explicit CamTrigger(QWidget *parent = 0);
    ~CamTrigger();

    void initRange();
    void updateValue();
    void updatedelayTime(uint exposureTime);
    void enableTriggerControl(bool enabled = false);

protected:
    void paintEvent(QPaintEvent *event);

private slots:
    void on_rbOff_clicked();

    void on_rbStandard_clicked();

    void on_rbSynchronization_clicked();

    void on_rbGlobal_clicked();

    void on_rbTimed_clicked();

    void on_rbWidth_clicked();

    void on_rbRising_clicked();

    void on_rbFalling_clicked();


    void on_cbSoftwareTrigger_clicked(bool checked);

    void on_pbOk_clicked();

    void on_pbSnap_clicked();

    void on_pbApply_clicked();

    void on_spbNum_editingFinished();

    void on_pbStop_clicked();

private:
    Ui::CamTrigger *ui;

    TUCAM_TRIGGER_ATTR m_triggerAttr;

    void setTrigger();

    void totalFrameVisiable();
};

#endif // CAMTRIGGER_H
