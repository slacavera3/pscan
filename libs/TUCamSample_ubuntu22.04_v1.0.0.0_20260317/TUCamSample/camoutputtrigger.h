#ifndef CAMOUTPUTTRIGGER_H
#define CAMOUTPUTTRIGGER_H

#include <QWidget>
#include "camobject.h"

namespace Ui {
class CamOutputTrigger;
}

typedef struct _tagTUCAM_PATAM_TRGOUTPUT
{
    int   nTgrOutMode;
    int   nEdgeMode;
    int   nDelayTm;
    int   nWidth;
}TUCAM_PATAM_TRGOUTPUT;

typedef struct _tagTUCAM_TRGOUTPUT
{
    int   nTgrOutPort;                           // [in/out] The port of triggerout
    TUCAM_PATAM_TRGOUTPUT TgrPort1;
    TUCAM_PATAM_TRGOUTPUT TgrPort2;
    TUCAM_PATAM_TRGOUTPUT TgrPort3;
}TUCAM_TRGOUTPUT;

class CamOutputTrigger : public QWidget
{
    Q_OBJECT
public:
    explicit CamOutputTrigger(QWidget *parent = nullptr);
    ~CamOutputTrigger();

    void initRange();
    void updateValue();

protected:
    void paintEvent(QPaintEvent *event);

private slots:
    void on_checkBoxEnable_clicked(bool checked);

    void on_comboBoxPort_currentIndexChanged(int index);

    void on_comboBoxKind_currentIndexChanged(int index);

    void on_rbRising_clicked();

    void on_rbFalling_clicked();

    void on_pbOk_clicked();

    void on_pbWOk_clicked();

private:
    Ui::CamOutputTrigger *ui;

    void UpdateDelay(int nDelay);
    void UpdateWidth(int nWidth);
    void UpdateRadio();
    void UpdateOutPutTgr();
    void UpdateTgrDelay();
    void EnableOutPutTgrControl(int nKind);

    TUCAM_TRGOUTPUT m_tgrOutAttr;
    TUCAM_TRGOUT_ATTR m_tgrOut;
};

#endif // CAMOUTPUTTRIGGER_H
